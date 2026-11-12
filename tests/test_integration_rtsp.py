"""Real libav against a real local RTSP server, standing in for the camera.

MediaMTX requires Digest auth like the Tapo does, and the publisher includes an
audio track that must never be ingested. Run with: pytest -m integration
Needs ffmpeg and mediamtx on PATH. Uses synthetic test patterns only.
"""

import logging
import os
import shutil
import signal
import socket
import subprocess
import textwrap
import time

import pytest
from conftest import wait_for

from acacia.config import CameraConfig
from acacia.credentials import CameraCredentials, RtspUrl
from acacia.ingest import IngestStatus, IngestWorker, LatestSlot, PyAvSource, classify_error
from acacia.probe import describe_tracks
from acacia.sanitize import clear_registered_secrets, register_secret

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (shutil.which("mediamtx") and shutil.which("ffmpeg")), reason="needs mediamtx and ffmpeg"),
]

PORT = 18554
USER = "cam@home"
PASSWORD = "p@ss#w$rd&1"
FAKE_WRONG = "wr0ng@pass#word"


def wait_port(port, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError("server did not open its port")


class Rig:
    def __init__(self, directory):
        self.config = directory / "mediamtx.yml"
        self.config.write_text(textwrap.dedent(f"""\
            logLevel: error
            api: no
            metrics: no
            pprof: no
            playback: no
            rtsp: yes
            rtspTransports: [tcp]
            rtspAddress: 127.0.0.1:{PORT}
            rtspAuthMethods: [digest]
            rtmp: no
            hls: no
            webrtc: no
            srt: no
            authMethod: internal
            authInternalUsers:
            - user: any
              permissions:
              - action: publish
            - user: '{USER}'
              pass: '{PASSWORD}'
              permissions:
              - action: read
            paths:
              all_others:
            """))
        self.server = None
        self.publisher = None

    def start_server(self):
        # MediaMTX writes a self-signed key into its working directory.
        self.server = subprocess.Popen(["mediamtx", str(self.config)], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, cwd=self.config.parent)
        wait_port(PORT)

    def start_publisher(self):
        self.publisher = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "15", "-pix_fmt", "yuv420p",
             "-c:a", "libopus", "-f", "rtsp", "-rtsp_transport", "tcp", f"rtsp://127.0.0.1:{PORT}/stream2"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)

    def stop_publisher(self):
        if self.publisher:
            self.publisher.kill()
            self.publisher.wait()
            self.publisher = None

    def stop_server(self):
        if self.server:
            self.server.kill()
            self.server.wait()
            self.server = None


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.start_server()
    r.start_publisher()
    yield r
    if r.server:
        os.kill(r.server.pid, signal.SIGCONT)
    r.stop_publisher()
    r.stop_server()
    clear_registered_secrets()


def camera(**overrides):
    values = dict(host="127.0.0.1", port=PORT, path="/stream2", open_timeout_s=4.0, read_timeout_s=1.0,
                  reconnect_max_backoff_s=1.0)
    values.update(overrides)
    return CameraConfig(**values)


def url(password=PASSWORD):
    return RtspUrl("127.0.0.1", PORT, "/stream2", CameraCredentials(USER, password))


def start_worker(cam=None, password=PASSWORD):
    cam = cam or camera()
    slot, status = LatestSlot(), IngestStatus()
    events = []
    thread = IngestWorker(cam, lambda: url(password), slot, status, lambda f: None,
                          on_event=lambda kind, reason: events.append((kind, reason)))
    thread.start()
    return thread, slot, status, events


def test_digest_auth_with_reserved_characters_decodes_video(rig):
    source = PyAvSource(url(), camera())
    try:
        assert source.info.codec == "h264"
        assert (source.info.width, source.info.height) == (640, 360)
        frames = source.frames()
        pixels, width, height, pts = next(frames)
        assert (width, height) == (640, 360)
        assert pixels.to_ndarray(format="rgb24").shape == (360, 640, 3)
        assert isinstance(pts, float)
    finally:
        source.close()


def test_audio_is_advertised_but_never_set_up(rig):
    tracks = describe_tracks("127.0.0.1", PORT, "/stream2", CameraCredentials(USER, PASSWORD))
    assert sorted(tracks["advertised_tracks"]) == ["audio", "video"]
    source = PyAvSource(url(), camera())
    try:
        assert source.info.video_tracks == 1
        assert source.info.audio_tracks_opened == 0
        assert len(source._container.streams) == 1
    finally:
        source.close()


def test_raw_libav_errors_leak_the_url_and_our_handling_does_not(rig, caplog):
    """The leak this guards against is real: PyAV puts the whole URL in its message."""
    register_secret(FAKE_WRONG)
    bad = url(FAKE_WRONG)
    with pytest.raises(Exception) as caught:
        PyAvSource(bad, camera())
    assert "wr0ng" in str(caught.value) or "%40" in str(caught.value)
    message = classify_error(caught.value)
    assert "wr0ng" not in message and "credentials" in message

    caplog.set_level(logging.DEBUG)
    thread, _, status, _ = start_worker(camera(auth_max_attempts=2), FAKE_WRONG)
    wait_for(lambda: status.phase == "auth_failed", 15, "auth pause")
    thread.stop()
    thread.join(3)
    combined = caplog.text + str(status.to_json())
    assert "wr0ng" not in combined and "pass%" not in combined and "cam%40home" not in combined


def test_publisher_restart_recovers_with_fresh_frames(rig):
    thread, slot, status, events = start_worker()
    try:
        wait_for(lambda: status.phase == "streaming" and slot.peek()[0] is not None, 8, "first frame")
        rig.stop_publisher()
        wait_for(lambda: status.phase != "streaming", 6, "loss detected")
        rig.start_publisher()
        wait_for(lambda: status.session >= 2 and status.phase == "streaming", 15, "reconnect")
        wait_for(lambda: time.monotonic() - slot.peek()[0].t_decoded < 0.5, 5, "fresh frame")
        assert status.reconnects >= 1
        assert slot.peek()[0].session >= 2
        assert any(kind == "stream_lost" for kind, _ in events)
    finally:
        thread.stop()
        thread.join(3)


def test_server_killed_mid_stream_then_stop_is_prompt(rig):
    thread, slot, status, _ = start_worker()
    wait_for(lambda: slot.peek()[0] is not None, 8, "first frame")
    rig.stop_server()
    wait_for(lambda: status.phase in ("reconnecting", "connecting"), 6, "loss detected")
    started = time.monotonic()
    thread.stop()
    thread.join(3)
    assert not thread.is_alive()
    assert time.monotonic() - started < 2.5


def test_frozen_server_trips_the_read_timeout(rig):
    """SIGSTOP leaves the TCP connection open with no data, like a hung camera."""
    thread, slot, status, events = start_worker()
    try:
        wait_for(lambda: slot.peek()[0] is not None, 8, "first frame")
        os.kill(rig.server.pid, signal.SIGSTOP)
        frozen_at = time.monotonic()
        wait_for(lambda: status.phase != "streaming", 6, "stall detected")
        assert time.monotonic() - frozen_at < 4.0
        os.kill(rig.server.pid, signal.SIGCONT)
        wait_for(lambda: status.session >= 2 and status.phase == "streaming", 15, "recovery")
        assert any(kind == "stream_lost" for kind, _ in events)
    finally:
        os.kill(rig.server.pid, signal.SIGCONT)
        thread.stop()
        thread.join(3)


def test_one_corrupt_packet_does_not_drop_the_session(monkeypatch):
    """Exercises the decode guard directly with a damaged packet."""
    import av

    class Packet:
        def __init__(self, bad):
            self.bad = bad

        def decode(self):
            if self.bad:
                raise av.error.InvalidDataError(1094995529, "bad data")
            return []

    class Container:
        def demux(self, _):
            return iter([Packet(True), Packet(False), Packet(True)])

    source = PyAvSource.__new__(PyAvSource)
    source._container, source._stream = Container(), None
    source._max_decode_gap_s, source.decode_errors = 60.0, 0
    assert list(source.frames()) == []
    assert source.decode_errors == 2

    source._max_decode_gap_s = -1.0  # nothing has decoded for too long
    source._container = Container()
    with pytest.raises(av.error.InvalidDataError):
        list(source.frames())


def test_slow_consumer_cannot_create_a_backlog(rig):
    thread, slot, status, _ = start_worker()
    try:
        wait_for(lambda: slot.peek()[0] is not None, 8, "first frame")
        time.sleep(3.0)  # nothing consumes during this time
        frame, _ = slot.peek()
        assert time.monotonic() - frame.t_decoded < 0.5
        assert slot.overwritten >= 20
        assert status.decoded_total >= 20
    finally:
        thread.stop()
        thread.join(3)
