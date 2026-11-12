"""Local web UI and API.

Binding to loopback does not stop a web page in the same browser from reaching
this server, and browsers do not apply same-origin rules to WebSockets. So every
request has its Host and Origin checked, which blocks cross-site reads of the
camera feed and DNS rebinding.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from PIL import Image
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from acacia.config import Config, ConfigError
from acacia.events import TRUTH_KINDS, local_timestamp
from acacia.geometry import CalibrationError
from acacia.ingest import Frame
from acacia.pipeline import Pipeline

log = logging.getLogger("acacia.web")

STATIC_DIR = Path(__file__).parent / "static"
DISPLAY_MAX_WIDTH = 1280
HEALTH_EVERY_S = 1.0
MAX_BODY_BYTES = 256_000


class LocalOnly:
    """ASGI guard: refuse any request not addressed to, or sent from, this app."""

    def __init__(self, app, hosts: set[str], origins: set[str]):
        self.app = app
        self.hosts = hosts
        self.origins = origins

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin1").lower()
        origin = headers.get(b"origin")
        allowed = host in self.hosts and (origin is None or origin.decode("latin1").lower() in self.origins)
        if allowed:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
        else:
            await Response("Forbidden", status_code=403)(scope, receive, send)


class SecurityHeaders:
    def __init__(self, app, policy: str):
        self.app = app
        self.headers = [
            (b"content-security-policy", policy.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"cache-control", b"no-store"),
        ]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(self.headers)
            await send(message)

        await self.app(scope, receive, wrapped)


class JpegCache:
    """Encode each frame at most once, however many tabs are open."""

    def __init__(self, quality: int):
        self.quality = quality
        self._lock = threading.Lock()
        self._seq = -1
        self._data = b""

    def encode(self, frame: Frame) -> bytes:
        with self._lock:
            if frame.seq == self._seq:
                return self._data
            image = Image.fromarray(frame.rgb())
            if image.width > DISPLAY_MAX_WIDTH:
                image = image.resize((DISPLAY_MAX_WIDTH, round(image.height * DISPLAY_MAX_WIDTH / image.width)),
                                     Image.BILINEAR)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=self.quality)
            self._seq, self._data = frame.seq, buffer.getvalue()
            return self._data


def _pack(header: dict, jpeg: bytes) -> bytes:
    encoded = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(encoded)) + encoded + jpeg


async def _json_body(request: Request) -> dict:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise CalibrationError("request too large")
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise CalibrationError("body must be JSON") from None
    if not isinstance(data, dict):
        raise CalibrationError("body must be a JSON object")
    return data


def create_app(pipeline: Pipeline, cfg: Config, extra_hosts: tuple[str, ...] = (),
               manage_pipeline: bool = True) -> Starlette:
    port = cfg.server.port
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", *extra_hosts}
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    sockets = f"ws://127.0.0.1:{port} ws://localhost:{port}"
    policy = (f"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
              f"connect-src 'self' {sockets}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    jpeg_cache = JpegCache(cfg.server.jpeg_quality)

    async def index(_: Request):
        return FileResponse(STATIC_DIR / "index.html")

    async def get_state(_: Request):
        return JSONResponse(pipeline.snapshot())

    async def get_health(_: Request):
        return JSONResponse(pipeline.health())

    async def get_events(request: Request):
        try:
            limit = int(request.query_params.get("limit", "200"))
        except ValueError:
            limit = 200
        tier = request.query_params.get("tier") or None
        return JSONResponse(pipeline.store.recent_events(limit, tier))

    async def get_calibration(_: Request):
        return JSONResponse(pipeline.calibration_json())

    async def put_calibration(request: Request):
        try:
            body = await _json_body(request)
            return JSONResponse(pipeline.save_polygons(body.get("polygons")))
        except CalibrationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    async def invalidate_calibration(request: Request):
        try:
            body = await _json_body(request)
        except CalibrationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse(pipeline.invalidate_calibration(str(body.get("reason", "camera moved"))))

    async def get_settings(_: Request):
        return JSONResponse(pipeline.settings_json())

    async def patch_settings(request: Request):
        try:
            body = await _json_body(request)
            return JSONResponse(pipeline.update_settings(body))
        except (ConfigError, CalibrationError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    async def get_truth(_: Request):
        return JSONResponse(pipeline.store.recent_truth())

    async def post_truth(request: Request):
        try:
            body = await _json_body(request)
        except CalibrationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        kind = body.get("kind")
        if kind not in TRUTH_KINDS:
            return JSONResponse({"error": "unknown mark"}, status_code=422)
        return JSONResponse(pipeline.store.add_truth(kind, str(body.get("note", ""))))

    async def get_cue(_: Request):
        return JSONResponse(pipeline.cue.status())

    async def post_cue(request: Request):
        try:
            body = await _json_body(request)
        except CalibrationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        if not isinstance(body.get("armed"), bool):
            return JSONResponse({"error": "armed must be true or false"}, status_code=422)
        try:
            pipeline.cue.set_armed(body["armed"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(pipeline.cue.status())

    async def post_retry(_: Request):
        pipeline.retry_ingest()
        return JSONResponse({"ok": True})

    async def live(socket: WebSocket):
        await socket.accept()
        sent_frames: OrderedDict[int, float] = OrderedDict()

        async def read_acks():
            while True:
                try:
                    message = json.loads(await socket.receive_text())
                except (ValueError, TypeError):
                    continue
                except WebSocketDisconnect:
                    return
                if not isinstance(message, dict):
                    continue
                now = time.monotonic()
                decoded_at = sent_frames.pop(message.get("frame"), None)
                if decoded_at is not None:
                    pipeline.metrics.frame_to_ui_ack_ms.add((now - decoded_at) * 1e3)
                pipeline.note_ui_ack(message.get("evidence"), now)

        reader = asyncio.create_task(read_acks())
        last_seq = -1
        last_health = 0.0
        interval = 1.0 / cfg.server.ui_fps
        try:
            while not reader.done():
                tick = time.monotonic()
                snapshot = pipeline.snapshot()
                snapshot["wall"] = local_timestamp()
                if tick - last_health >= HEALTH_EVERY_S:
                    snapshot["health"] = pipeline.health()
                    last_health = tick
                frame, _ = pipeline.frames.peek()
                jpeg = b""
                fresh = frame is not None and tick - frame.t_decoded <= cfg.state.stale_timeout_s
                if fresh and frame.seq != last_seq:
                    jpeg = await asyncio.to_thread(jpeg_cache.encode, frame)
                    last_seq = frame.seq
                    sent_frames[frame.seq] = frame.t_decoded
                    while len(sent_frames) > 64:
                        sent_frames.popitem(last=False)
                    pipeline.metrics.ui_frames.tick(tick)
                await socket.send_bytes(_pack(snapshot, jpeg))
                await asyncio.sleep(max(0.0, interval - (time.monotonic() - tick)))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            reader.cancel()

    @asynccontextmanager
    async def lifespan(_: Starlette):
        if manage_pipeline:
            pipeline.start()
        yield
        if manage_pipeline:
            await asyncio.to_thread(pipeline.stop)

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/state", get_state),
            Route("/api/health", get_health),
            Route("/api/events", get_events),
            Route("/api/calibration", get_calibration),
            Route("/api/calibration", put_calibration, methods=["PUT"]),
            Route("/api/calibration/invalidate", invalidate_calibration, methods=["POST"]),
            Route("/api/settings", get_settings),
            Route("/api/settings", patch_settings, methods=["PATCH"]),
            Route("/api/truth", get_truth),
            Route("/api/truth", post_truth, methods=["POST"]),
            Route("/api/retry", post_retry, methods=["POST"]),
            Route("/api/cue", get_cue),
            Route("/api/cue", post_cue, methods=["POST"]),
            WebSocketRoute("/ws", live),
            Mount("/static", StaticFiles(directory=STATIC_DIR)),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeaders, policy=policy)
    app.add_middleware(LocalOnly, hosts=hosts, origins=origins)
    return app
