"""Replay on a small generated clip: synthetic pixels, scripted detections."""

import av
import numpy as np
import pytest
from conftest import ScriptedDetector, dog

from acacia.config import Config
from acacia.detector import DetectionResult
from acacia.geometry import Calibration, Polygon, View
from acacia.replay import format_report, replay_clip

SEAT = [[0.2, 0.4], [0.9, 0.4], [0.9, 0.62], [0.2, 0.62]]
ON_BOX = (0.45, 0.25, 0.55, 0.58)
FLOOR_BOX = (0.30, 0.75, 0.42, 0.95)


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    with av.open(str(path), "w") as out:
        stream = out.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "yuv420p"
        for i in range(300):  # 30 seconds
            frame = av.VideoFrame.from_ndarray(np.full((180, 320, 3), i % 255, dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    return path


class TimedDetector(ScriptedDetector):
    """Dog on the seat from 5 to 15 seconds of the clip, then on the floor."""

    def __init__(self, interval):
        super().__init__()
        self.calls = 0
        self.interval = interval

    def detect(self, rgb, thresholds):
        t = self.calls * self.interval
        self.calls += 1
        if t < 5:
            found = ()
        elif t < 15:
            found = (dog(ON_BOX),)
        else:
            found = (dog(FLOOR_BOX),)
        return DetectionResult(found, 0, 0, 0)


def calibrated():
    return Calibration(polygons=[Polygon("a", "seat", True, SEAT)],
                       view=View(320, 180, 0, Config().camera.path))


def test_clip_drives_the_real_state_machine_in_video_time(clip):
    report = replay_clip(clip, TimedDetector(0.5), Config(), calibrated(), interval_s=0.5)
    kinds = [(round(t), e.kind) for t, e in report.events]
    assert (5, "candidate_started") in kinds
    assert (8, "confirmed_on") in kinds  # 5 s plus the 3 s confirm interval
    assert (20, "confirmed_clear") in kinds  # 15 s plus the 5 s clear interval
    assert report.final_state == "OFF"


def test_without_polygons_it_reports_detections_only(clip):
    report = replay_clip(clip, TimedDetector(0.5), Config(), Calibration(), interval_s=0.5)
    assert report.events == [] and report.final_state is None
    assert "calibration missing" in report.calibration_note
    text = format_report(report, Config())
    assert "Dog at or above 0.4" in text and "One clip cannot establish" in text


def test_gap_reporting_exposes_detector_dropouts(clip):
    class Flicker(ScriptedDetector):
        calls = 0

        def detect(self, rgb, thresholds):
            self.calls += 1
            t = self.calls * 0.5
            return DetectionResult((dog(ON_BOX),) if not 10 <= t < 14 else (), 0, 0, 0)

    report = replay_clip(clip, Flicker(), Config(), Calibration(), interval_s=0.5)
    assert "longest 4.5 s, 1 longer than evidence_gap_s" in format_report(report, Config())


def test_a_missing_clip_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="nope.mp4"):
        replay_clip(tmp_path / "nope.mp4", ScriptedDetector(), Config(), Calibration())


def test_replay_writes_nothing(clip, tmp_path):
    before = set(tmp_path.rglob("*"))
    replay_clip(clip, TimedDetector(0.5), Config(), Calibration(), interval_s=1.0)
    assert set(tmp_path.rglob("*")) == before
