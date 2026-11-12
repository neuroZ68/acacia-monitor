"""Video-only RTSP ingest with a bounded latest-frame handoff.

The decoder must see every packet, but nothing downstream has to. Frames go into
a single overwrite slot, so a slow consumer loses old frames instead of building
a backlog. libav runs in-process, which keeps the authenticated URL out of any
subprocess argument list.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

import av
import av.error as ave
import numpy as np

from acacia.config import CameraConfig
from acacia.credentials import CredentialError, RtspUrl
from acacia.sanitize import scrub

log = logging.getLogger("acacia.ingest")
T = TypeVar("T")

# Baseline creep in seconds per second, so slow clock drift between the camera
# and this Mac is not mistaken for backlog.
BASELINE_CREEP = 0.001
# A larger jump means the camera timeline restarted, not that frames are late.
TIMELINE_RESET_S = 60.0


class LatestSlot(Generic[T]):
    """One-item overwrite buffer. Memory use cannot grow with consumer lag."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: T | None = None
        self._version = 0
        self._taken_version = 0
        self.overwritten = 0

    def put(self, item: T) -> None:
        with self._cond:
            if self._item is not None and self._taken_version < self._version:
                self.overwritten += 1
            self._item = item
            self._version += 1
            self._cond.notify_all()

    def peek(self) -> tuple[T | None, int]:
        with self._cond:
            return self._item, self._version

    def take_newer(self, version: int, timeout: float) -> tuple[T | None, int]:
        """Wait for something newer than `version` and mark it consumed."""
        with self._cond:
            if self._version <= version:
                self._cond.wait(timeout)
            if self._version <= version:
                return None, version
            self._taken_version = self._version
            return self._item, self._version

    def clear(self) -> None:
        with self._cond:
            self._item = None


class Frame:
    """A decoded frame. Pixel conversion is lazy and only paid by consumers."""

    __slots__ = ("seq", "session", "width", "height", "t_decoded", "backlog_s", "_source", "_rotation", "_rgb",
                 "_lock")

    def __init__(self, seq: int, session: int, source, width: int, height: int,
                 t_decoded: float, backlog_s: float, rotation_deg: int = 0):
        self.seq = seq
        self.session = session
        self._source = source
        self._rotation = rotation_deg
        swap = rotation_deg in (90, 270)
        self.width = height if swap else width
        self.height = width if swap else height
        self.t_decoded = t_decoded
        self.backlog_s = backlog_s
        self._rgb: np.ndarray | None = None
        self._lock = threading.Lock()  # inference and the UI can both ask at once

    def rgb(self) -> np.ndarray:
        with self._lock:
            if self._rgb is None:
                source = self._source
                array = source if isinstance(source, np.ndarray) else source.to_ndarray(format="rgb24")
                if self._rotation:
                    array = np.ascontiguousarray(np.rot90(array, k=-(self._rotation // 90)))
                self._rgb = array
                self._source = None
            return self._rgb


class BacklogTracker:
    """Estimates how late a frame is relative to the stream's own timeline.

    Arrival age alone cannot spot replayed frames: after a stall, TCP delivers a
    burst of old frames that all look brand new locally. Comparing the arrival
    time with the presentation timestamp does reveal them.
    """

    def __init__(self) -> None:
        self.baseline: float | None = None
        self.last_t: float | None = None

    def update(self, t_arrival: float, pts_s: float | None) -> float:
        if pts_s is None:
            return 0.0
        lag = t_arrival - pts_s
        if self.baseline is None or self.last_t is None:
            self.baseline, self.last_t = lag, t_arrival
            return 0.0
        self.baseline += BASELINE_CREEP * max(0.0, t_arrival - self.last_t)
        self.last_t = t_arrival
        if abs(lag - self.baseline) > TIMELINE_RESET_S:
            self.baseline = lag
            return 0.0
        self.baseline = min(self.baseline, lag)
        return lag - self.baseline


@dataclass(frozen=True)
class StreamInfo:
    codec: str
    width: int
    height: int
    pixel_format: str
    declared_fps: float | None
    video_tracks: int
    audio_tracks_opened: int
    transport: str


class AuthFailed(Exception):
    pass


class StreamEnded(Exception):
    pass


class VideoSource(Protocol):
    info: StreamInfo

    def frames(self) -> Iterator[tuple[object, int, int, float | None]]:
        """Yield (pixels, width, height, pts seconds)."""
        ...

    def close(self) -> None:
        ...


class PyAvSource:
    def __init__(self, url: RtspUrl, cam: CameraConfig):
        options = {
            "rtsp_transport": cam.transport,
            # Audio is never set up on the wire, so it is not merely discarded.
            "allowed_media_types": "video",
            "fflags": "nobuffer",
            "flags": "low_delay",
            "max_delay": "200000",
            "analyzeduration": "1000000",
            "probesize": "1000000",
            "user_agent": "acacia-camera",
        }
        self._max_decode_gap_s = cam.read_timeout_s
        self.decode_errors = 0
        self._container = av.open(url.reveal(), mode="r", options=options,
                                  timeout=(cam.open_timeout_s, cam.read_timeout_s))
        try:
            self._stream = self._container.streams.video[0]
            context = self._stream.codec_context
            # Frame threading adds a full frame of delay per thread.
            context.thread_type = "SLICE"
            context.flags |= av.codec.context.Flags.low_delay
            rate = self._stream.average_rate or self._stream.guessed_rate
            self.info = StreamInfo(
                codec=context.name,
                width=context.width,
                height=context.height,
                pixel_format=str(context.pix_fmt),
                declared_fps=float(rate) if rate else None,
                video_tracks=len(self._container.streams.video),
                audio_tracks_opened=len(self._container.streams.audio),
                transport=cam.transport,
            )
        except Exception:
            self.close()
            raise

    def frames(self):
        last_good = time.monotonic()
        for packet in self._container.demux(self._stream):
            try:
                decoded = packet.decode()
            except ave.InvalidDataError:
                # One damaged packet is survivable, but packets that never decode
                # would otherwise look like a live stream forever.
                self.decode_errors += 1
                if time.monotonic() - last_good > self._max_decode_gap_s:
                    raise
                continue
            for frame in decoded:
                last_good = time.monotonic()
                pts = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else None
                yield frame, frame.width, frame.height, pts

    def close(self) -> None:
        try:
            self._container.close()
        except Exception:  # noqa: BLE001 - closing a broken stream may fail; nothing to do
            pass


class IngestStatus:
    """Mutable ingest status, read by health reporting."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.phase = "idle"
        self.last_error = ""
        self.reconnects = 0
        self.session = 0
        self.session_started: float | None = None
        self.first_frame_at: float | None = None
        self.info: StreamInfo | None = None
        self.next_retry_s: float | None = None
        self.decoded_total = 0
        self.decode_errors = 0

    def set(self, **values) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def to_json(self) -> dict:
        with self.lock:
            info = self.info
            return {
                "phase": self.phase,
                "last_error": self.last_error,
                "reconnects": self.reconnects,
                "session": self.session,
                "decoded_total": self.decoded_total,
                "decode_errors": self.decode_errors,
                "next_retry_s": self.next_retry_s,
                "stream": None if info is None else info.__dict__,
            }


def classify_error(exc: BaseException) -> str:
    """Map a libav failure to short, sanitized user guidance."""
    if isinstance(exc, (ave.HTTPUnauthorizedError, ave.HTTPForbiddenError)):
        return "Camera rejected the credentials. Run ./acacia credentials set, then press Retry."
    if isinstance(exc, ave.HTTPNotFoundError):
        return "Camera has no such stream path. Check camera.path."
    if isinstance(exc, (ave.TimeoutError, ave.ExitError, TimeoutError)):
        return "Timed out waiting for the camera."
    if isinstance(exc, ave.ConnectionRefusedError):
        return "Camera refused the connection."
    if isinstance(exc, (ave.EOFError, StreamEnded)):
        return "Stream ended."
    return scrub(f"{type(exc).__name__}: {exc}")


def is_auth_error(exc: BaseException) -> bool:
    return isinstance(exc, (AuthFailed, ave.HTTPUnauthorizedError, ave.HTTPForbiddenError))


class IngestWorker(threading.Thread):
    def __init__(self, cam: CameraConfig, url_provider: Callable[[], RtspUrl], slot: LatestSlot[Frame],
                 status: IngestStatus, on_frame: Callable[[Frame], None],
                 source_factory: Callable[[RtspUrl, CameraConfig], VideoSource] = PyAvSource,
                 on_event: Callable[[str, str], None] = lambda kind, reason: None):
        super().__init__(name="ingest", daemon=True)
        self.cam = cam
        self.url_provider = url_provider
        self.slot = slot
        self.status = status
        self.on_frame = on_frame
        self.on_event = on_event
        self.source_factory = source_factory
        self.stop_event = threading.Event()
        self.retry_event = threading.Event()
        self.reconnect_event = threading.Event()
        self._seq = 0

    def stop(self) -> None:
        self.stop_event.set()
        self.retry_event.set()

    def retry(self) -> None:
        """Clear an auth pause or force a reconnect, from the UI."""
        self.reconnect_event.set()
        self.retry_event.set()

    def _backoff_wait(self, backoff: float) -> float:
        """Sleep with jitter, never beyond the cap, and return the next backoff."""
        cap = self.cam.reconnect_max_backoff_s
        self._sleep(min(cap, backoff * random.uniform(0.8, 1.2)))
        return min(backoff * 2, cap)

    def _sleep(self, seconds: float) -> None:
        self.status.set(next_retry_s=round(seconds, 1))
        self.retry_event.wait(seconds)
        self.retry_event.clear()
        self.status.set(next_retry_s=None)

    def run(self) -> None:
        backoff = 1.0
        auth_failures = 0
        while not self.stop_event.is_set():
            self.status.set(phase="connecting")
            self.reconnect_event.clear()
            try:
                url = self.url_provider()  # re-read, so rotated credentials apply on retry
                source = self.source_factory(url, self.cam)
            except CredentialError as exc:
                self.status.set(phase="no_credentials", last_error=str(exc))
                self._sleep(self.cam.reconnect_max_backoff_s)
                continue
            except Exception as exc:  # noqa: BLE001 - every failure takes the retry path
                message = classify_error(exc)
                self.status.set(last_error=message)
                log.warning("connect failed: %s", message)
                if is_auth_error(exc):
                    auth_failures += 1
                    if auth_failures >= self.cam.auth_max_attempts:
                        # Holding here avoids tripping a camera lockout.
                        self.status.set(phase="auth_failed")
                        self.on_event("auth_paused", "waiting for Retry")
                        self.retry_event.wait()
                        self.retry_event.clear()
                        auth_failures = 0
                        continue
                self.status.set(phase="reconnecting")
                backoff = self._backoff_wait(backoff)
                continue

            try:
                self._stream(source)
                backoff, auth_failures = 1.0, 0
            except Exception as exc:  # noqa: BLE001
                if not self.stop_event.is_set():
                    message = classify_error(exc)
                    self.status.set(last_error=message)
                    self.on_event("stream_lost", message)
                    log.warning("stream lost: %s", message)
            finally:
                source.close()

            if self.stop_event.is_set():
                break
            with self.status.lock:
                self.status.reconnects += 1
            self.status.set(phase="reconnecting")
            backoff = self._backoff_wait(backoff)
        self.status.set(phase="stopped")

    def _stream(self, source: VideoSource) -> None:
        with self.status.lock:
            self.status.session += 1
            session = self.status.session
            self.status.session_started = time.monotonic()
            self.status.first_frame_at = None
            self.status.info = source.info
        self.status.set(phase="streaming")
        self.on_event("stream_connected", f"{source.info.codec} {source.info.width}x{source.info.height}")
        tracker = BacklogTracker()
        for pixels, width, height, pts in source.frames():
            if self.stop_event.is_set() or self.reconnect_event.is_set():
                return
            now = time.monotonic()
            self._seq += 1
            frame = Frame(self._seq, session, pixels, width, height, now,
                          tracker.update(now, pts), self.cam.rotation_deg)
            self.slot.put(frame)
            with self.status.lock:
                self.status.decoded_total += 1
                self.status.decode_errors = getattr(source, "decode_errors", 0)
                if self.status.first_frame_at is None:
                    self.status.first_frame_at = now
            self.on_frame(frame)
        raise StreamEnded()
