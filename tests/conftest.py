"""Shared fakes. Everything here is synthetic: no camera, model, or stored images."""

from __future__ import annotations

import time
from dataclasses import replace

import numpy as np
import pytest

from acacia.config import CameraConfig, Config, ServerConfig, StateConfig
from acacia.detector import Detection, DetectionResult
from acacia.ingest import StreamInfo
from acacia.pipeline import Paths

INFO = StreamInfo("h264", 640, 360, "yuv420p", 15.0, 1, 0, "tcp")


class FakeSource:
    """Yields synthetic frames at a fixed rate until told to stall or end."""

    info = INFO

    def __init__(self, url=None, cam=None, fps=30.0, frames=None, stall_after=None):
        self.fps = fps
        self.limit = frames
        self.stall_after = stall_after
        self.closed = False
        self.pixels = np.zeros((360, 640, 3), dtype=np.uint8)

    def frames(self):
        count = 0
        while self.limit is None or count < self.limit:
            if self.stall_after is not None and count >= self.stall_after:
                time.sleep(0.05)
                continue
            time.sleep(1 / self.fps)
            count += 1
            yield self.pixels, 640, 360, count / self.fps

    def close(self):
        self.closed = True


class ScriptedDetector:
    """Returns whatever the test last assigned to `detections`."""

    name = "scripted"
    provider = "fake"

    def __init__(self):
        self.detections: list[Detection] = []
        self.error: Exception | None = None
        self.hang = False
        self.closed = False

    def detect(self, rgb, thresholds):
        if self.error:
            raise self.error
        while self.hang:
            time.sleep(0.01)
        return DetectionResult(tuple(self.detections), 1.0, 2.0, 0.5)

    def close(self):
        self.closed = True


def dog(box, confidence=0.9):
    return Detection("dog", confidence, box)


def person(box, confidence=0.9):
    return Detection("person", confidence, box)


@pytest.fixture
def state_cfg():
    return StateConfig(on_confirm_s=3.0, clear_confirm_s=5.0, lost_clear_s=60.0,
                       evidence_gap_s=1.5, hold_s=10.0, stale_timeout_s=3.0, recovery_warmup_s=2.0)


@pytest.fixture
def fast_config():
    """Short timers, so threaded tests finish quickly."""
    state = StateConfig(on_confirm_s=0.3, clear_confirm_s=0.4, lost_clear_s=1.0, evidence_gap_s=0.3,
                        hold_s=0.6, stale_timeout_s=0.5, recovery_warmup_s=0.1)
    config = Config(camera=CameraConfig(host="192.0.2.1", read_timeout_s=0.5), state=state,
                    server=ServerConfig(port=18765))
    return replace(config, detector=replace(config.detector, inference_interval_s=0.03))


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path, tmp_path / "data", tmp_path / "models")


def wait_for(condition, timeout=5.0, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {message}")
