"""Measure a stream's real properties instead of assuming them from its name."""

from __future__ import annotations

import hashlib
import re
import socket
import statistics
import time
from pathlib import Path

from acacia.config import Config
from acacia.credentials import CameraCredentials, RtspUrl, load_credentials
from acacia.ingest import PyAvSource, classify_error


def _digest_header(challenge: str, creds: CameraCredentials, uri: str) -> str:
    realm = re.search(r'realm="([^"]*)"', challenge).group(1)
    nonce = re.search(r'nonce="([^"]*)"', challenge).group(1)
    ha1 = hashlib.md5(f"{creds.username}:{realm}:{creds.password}".encode()).hexdigest()  # noqa: S324
    ha2 = hashlib.md5(f"DESCRIBE:{uri}".encode()).hexdigest()  # noqa: S324
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()  # noqa: S324
    return (f'Digest username="{creds.username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}"')


def describe_tracks(host: str, port: int, path: str, creds: CameraCredentials) -> dict:
    """List advertised tracks using DESCRIBE alone, so no media or audio ever flows.

    The hand-rolled request exists because opening with libav would have to SET UP
    an audio track just to learn that it exists.
    """
    uri = f"rtsp://{host}:{port}{path}"

    def request(sock: socket.socket, extra: str, cseq: int) -> str:
        sock.sendall(f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\n"
                     f"User-Agent: acacia-camera\r\n{extra}\r\n".encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        match = re.search(rb"(?i)content-length:\s*(\d+)", head)
        length = int(match.group(1)) if match else 0
        while len(body) < length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
        return (head + b"\r\n\r\n" + body).decode("utf-8", "replace")

    # One connection for both requests: a Digest nonce is tied to its connection.
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(5)
        first = request(sock, "", 1)
        challenge = re.search(r"(?i)WWW-Authenticate:\s*(Digest [^\r\n]*)", first)
        if not challenge:
            return {"error": "camera offered no Digest challenge"}
        reply = request(sock, f"Authorization: {_digest_header(challenge.group(1), creds, uri)}\r\n", 2)
    status = reply.split("\r\n", 1)[0]
    if " 200 " not in status:
        return {"error": f"DESCRIBE failed: {status}"}
    media = re.findall(r"(?m)^m=(\w+) .*$", reply)
    return {
        "advertised_tracks": media,
        "codecs": re.findall(r"(?m)^a=rtpmap:\d+ ([^\r\n]+)", reply),
        "advertised_framerate": re.findall(r"(?m)^a=framerate:([^\r\n]+)", reply),
    }


def run_probe(cfg: Config, root: Path, stream: str, seconds: float, transport: str) -> dict:
    from dataclasses import replace

    conf = cfg.credentials
    creds = load_credentials(conf.backend, conf.service, root / conf.file)
    cam = replace(cfg.camera, path=stream, transport=transport)
    report: dict = {
        "method": "RTSP DESCRIBE, then PyAV video-only decode with arrival timing on this Mac",
        "host": cam.host,
        "stream": stream,
        "transport": transport,
        "requested_seconds": seconds,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sdp": describe_tracks(cam.host, cam.port, stream, creds),
    }
    url = RtspUrl(cam.host, cam.port, stream, creds)
    opened = time.monotonic()
    try:
        source = PyAvSource(url, cam)
    except Exception as exc:  # noqa: BLE001 - reported, sanitized
        report["error"] = classify_error(exc)
        return report
    report["open_s"] = round(time.monotonic() - opened, 3)
    report["stream_info"] = source.info.__dict__

    arrivals: list[float] = []
    keyframes: list[float] = []
    packet_bytes = 0
    started = time.monotonic()
    try:
        for packet in source._container.demux(source._stream):
            packet_bytes += packet.size
            for frame in packet.decode():
                now = time.monotonic()
                arrivals.append(now)
                if frame.key_frame:
                    keyframes.append(now)
                if "width" not in report:
                    report["width"], report["height"] = frame.width, frame.height
                    report["first_frame_s"] = round(now - opened, 3)
            if time.monotonic() - started >= seconds:
                break
    except Exception as exc:  # noqa: BLE001
        report["error"] = classify_error(exc)
    finally:
        source.close()

    report["frames"] = len(arrivals)
    if len(arrivals) >= 2:
        gaps = sorted((b - a) * 1e3 for a, b in zip(arrivals, arrivals[1:]))
        span = arrivals[-1] - arrivals[0]
        report["measured_fps"] = round((len(arrivals) - 1) / span, 2)
        report["frame_gap_ms"] = {
            "median": round(statistics.median(gaps), 1),
            "p95": round(gaps[int(len(gaps) * 0.95)], 1),
            "max": round(gaps[-1], 1),
        }
        report["approx_kbit_per_s"] = round(packet_bytes * 8 / max(span, 0.001) / 1000)
    if len(keyframes) >= 2:
        report["keyframe_interval_s"] = round(statistics.median(b - a for a, b in zip(keyframes, keyframes[1:])), 2)
    return report
