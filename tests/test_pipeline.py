"""The real threads and state machine, with a fake camera and detector."""

import json
import time
from dataclasses import replace

import numpy as np
import pytest
from conftest import FakeSource, ScriptedDetector, dog, person, wait_for

from acacia.detector import DetectorError
from acacia.geometry import CalibrationError
from acacia.pipeline import Pipeline, scene_stats, view_mode

SEAT = [[0.2, 0.4], [0.9, 0.4], [0.9, 0.62], [0.2, 0.62]]
ON_BOX = (0.45, 0.25, 0.55, 0.58)
FLOOR_BOX = (0.30, 0.75, 0.42, 0.95)


class Rig:
    def __init__(self, cfg, paths, source=None, detector_factory=None):
        self.detector = ScriptedDetector()
        self.source = source or FakeSource(fps=60)
        self.pipeline = Pipeline(cfg, paths, detector_factory or (lambda c: self.detector),
                                 source_factory=lambda u, c: self.source)
        self.pipeline._url = lambda: None

    def state(self):
        return self.pipeline.machine.state.value

    def reason(self):
        return self.pipeline.machine.reason

    def calibrate(self):
        wait_for(lambda: self.pipeline._live_view is not None, message="live view")
        self.pipeline.save_polygons([{"name": "seat", "points": SEAT}])

    def kinds(self):
        return [e["kind"] for e in reversed(self.pipeline.store.recent_events())]


@pytest.fixture
def rig(fast_config, paths):
    r = Rig(fast_config, paths)
    r.pipeline.start()
    yield r
    r.pipeline.stop()


def test_starts_unknown_until_calibrated(rig):
    assert rig.state() == "UNKNOWN"
    wait_for(lambda: rig.reason() == "calibration_missing", message="calibration reason")
    assert rig.state() == "UNKNOWN"


def test_cannot_calibrate_blind(fast_config, paths):
    r = Rig(fast_config, paths)
    with pytest.raises(CalibrationError, match="live video"):
        r.pipeline.save_polygons([{"points": SEAT}])
    r.pipeline.store.close()


def test_full_cycle_off_on_clear(rig):
    rig.calibrate()
    wait_for(lambda: rig.state() == "OFF", message="OFF")
    rig.detector.detections = [dog(ON_BOX)]
    wait_for(lambda: rig.state() == "ON", message="ON")
    rig.detector.detections = [dog(FLOOR_BOX)]
    wait_for(lambda: rig.state() == "OFF", message="clear")
    kinds = rig.kinds()
    assert kinds.count("confirmed_on") == 1
    assert kinds.count("confirmed_clear") == 1
    assert kinds.index("candidate_started") < kinds.index("confirmed_on") < kinds.index("confirmed_clear")


def test_people_alone_do_not_trigger_or_suppress(rig):
    rig.calibrate()
    wait_for(lambda: rig.state() == "OFF", message="OFF")
    rig.detector.detections = [person((0.75, 0.3, 0.92, 0.9))]
    time.sleep(0.3)
    assert rig.state() == "OFF"
    rig.detector.detections = [person((0.75, 0.3, 0.92, 0.9)), dog(ON_BOX)]
    wait_for(lambda: rig.state() == "ON", message="ON with a person present")


def test_stalled_stream_goes_unknown_never_clear(fast_config, paths):
    source = FakeSource(fps=60, stall_after=30)
    r = Rig(fast_config, paths, source=source)
    r.pipeline.start()
    try:
        r.calibrate()
        r.detector.detections = [dog(ON_BOX)]
        wait_for(lambda: r.reason() == "stale_stream", message="stale detection")
        assert r.state() == "UNKNOWN"
        time.sleep(0.2)
        assert "confirmed_clear" not in r.kinds()
    finally:
        source.stall_after = None
        r.pipeline.stop()


def test_detector_exception_goes_unknown_and_recovers(rig):
    rig.calibrate()
    wait_for(lambda: rig.state() == "OFF", message="OFF")
    rig.detector.error = RuntimeError("model blew up")
    wait_for(lambda: rig.state() == "UNKNOWN", message="UNKNOWN after failure")
    assert rig.reason() == "inference_failed_or_stalled"
    assert rig.pipeline.metrics.inference_failures > 0
    rig.detector.error = None
    wait_for(lambda: rig.state() == "OFF", message="recovery")
    assert "recovered" in rig.kinds()


def test_hung_detector_still_trips_the_watchdog(rig):
    rig.calibrate()
    wait_for(lambda: rig.state() == "OFF", message="OFF")
    rig.detector.hang = True
    wait_for(lambda: rig.state() == "UNKNOWN", message="watchdog")
    rig.detector.hang = False


def test_detector_that_cannot_load_is_visible(fast_config, paths):
    def broken(_):
        raise DetectorError("Model missing")

    r = Rig(fast_config, paths, detector_factory=broken)
    r.pipeline.start()
    try:
        r.calibrate()
        wait_for(lambda: r.reason() == "detector_unavailable", message="detector fault")
        assert r.state() == "UNKNOWN"
        assert "Model missing" in r.pipeline.health()["detector"]["error"]
    finally:
        r.pipeline.stop()


def test_invalidating_calibration_forces_unknown_until_redrawn(rig):
    rig.calibrate()
    wait_for(lambda: rig.state() == "OFF", message="OFF")
    rig.pipeline.invalidate_calibration("camera moved")
    wait_for(lambda: rig.state() == "UNKNOWN", message="UNKNOWN")
    assert rig.reason() == "calibration_invalid"
    rig.calibrate()
    wait_for(lambda: rig.state() == "OFF", message="OFF after redraw")


def test_calibration_persists_across_restart(fast_config, paths):
    first = Rig(fast_config, paths)
    first.pipeline.start()
    first.calibrate()
    first.pipeline.stop()
    second = Rig(fast_config, paths)
    second.pipeline.start()
    try:
        assert second.state() == "UNKNOWN"  # a restart never trusts old state
        wait_for(lambda: second.state() == "OFF", message="OFF after restart")
        assert len(second.pipeline.calibration.polygons) == 1
    finally:
        second.pipeline.stop()


def test_settings_apply_live_and_persist(rig):
    rig.pipeline.update_settings({"state.on_confirm_s": 7})
    assert rig.pipeline.machine.cfg.on_confirm_s == 7.0
    assert '"state.on_confirm_s": 7' in rig.pipeline.paths.settings.read_text()


def test_stop_releases_everything(fast_config, paths):
    r = Rig(fast_config, paths)
    r.pipeline.start()
    wait_for(lambda: r.pipeline.frames.peek()[0] is not None, message="frames")
    wait_for(lambda: r.pipeline._detector is not None, message="detector")
    started = time.monotonic()
    r.pipeline.stop()
    assert time.monotonic() - started < 2
    assert all(not t.is_alive() for t in r.pipeline._threads)
    assert r.detector.closed
    assert r.source.closed
    assert r.pipeline.frames.peek()[0] is None


def test_nothing_but_text_is_written_to_disk(rig, paths):
    rig.calibrate()
    wait_for(lambda: rig.state() == "OFF", message="OFF")
    suffixes = {p.suffix for p in paths.data.rglob("*") if p.is_file()}
    assert suffixes <= {".json", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".log"}.union({"", ".sqlite3-journal"})


def test_detections_show_before_calibration_without_a_classification(rig):
    rig.detector.detections = [dog(ON_BOX), person((0.1, 0.1, 0.3, 0.9))]
    wait_for(lambda: rig.pipeline.snapshot()["unclassified_dogs"], message="raw detections")
    snapshot = rig.pipeline.snapshot()
    assert snapshot["state"]["state"] == "UNKNOWN"
    assert snapshot["evidence"] is None
    assert snapshot["unclassified_dogs"][0]["confidence"] == 0.9
    assert len(snapshot["persons"]) == 1


def test_classified_dogs_are_not_also_listed_as_unclassified(rig):
    rig.calibrate()
    rig.detector.detections = [dog(ON_BOX)]
    wait_for(lambda: rig.pipeline.snapshot()["evidence"], message="evidence")
    wait_for(lambda: rig.pipeline.snapshot()["evidence"]["dogs"], message="classified dog")
    assert rig.pipeline.snapshot()["unclassified_dogs"] == []


def test_stale_detections_are_not_presented(fast_config, paths):
    r = Rig(fast_config, paths, source=FakeSource(fps=60, stall_after=30))
    r.detector.detections = [dog(ON_BOX)]
    r.pipeline.start()
    try:
        wait_for(lambda: r.pipeline.snapshot()["unclassified_dogs"], message="detections")
        wait_for(lambda: not r.pipeline.snapshot()["unclassified_dogs"], message="old detections removed")
        assert r.pipeline.snapshot()["scene"] is None
    finally:
        r.source.stall_after = None
        r.pipeline.stop()


def test_scene_stats_separate_infrared_from_colour():
    grey = np.full((360, 640, 3), 90, dtype=np.uint8)
    colour = np.zeros((360, 640, 3), dtype=np.uint8)
    colour[..., 0], colour[..., 1], colour[..., 2] = 150, 110, 80
    assert view_mode(scene_stats(grey)[1]) == "infrared"
    assert view_mode(scene_stats(colour)[1]) == "colour"
    assert scene_stats(grey)[0] == 90.0


def test_events_are_tagged_with_the_lighting_mode(rig):
    rig.calibrate()
    wait_for(lambda: rig.state() == "OFF", message="OFF")
    event = [e for e in rig.pipeline.store.recent_events() if e["kind"] == "clear_established"][0]
    assert json.loads(event["detail"])["view_mode"] == "infrared"  # the fake frames are black


def test_each_run_records_its_model_and_settings(rig):
    started = [e for e in rig.pipeline.store.recent_events() if e["kind"] == "app_started"][0]
    detail = json.loads(started["detail"])
    assert detail["model"] == "yolox_m"
    assert detail["settings"]["state.on_confirm_s"] == 0.3


def test_audio_status_is_read_from_the_stream(rig):
    wait_for(lambda: rig.pipeline.ingest_status.info is not None, message="stream info")
    assert rig.pipeline.health()["privacy"]["audio_ingest"] is False
    rig.pipeline.ingest_status.set(info=replace(rig.pipeline.ingest_status.info, audio_tracks_opened=1))
    assert rig.pipeline.health()["privacy"]["audio_ingest"] is True


def test_timing_metrics_are_populated(rig):
    rig.calibrate()
    wait_for(lambda: rig.state() == "OFF", message="OFF")
    health = rig.pipeline.health()
    timing = health["timing"]
    assert timing["decoded_fps"] > 0 and timing["inference_fps"] > 0
    assert timing["decode_to_decision_ms"]["n"] > 0
    assert health["privacy"] == {"audio_ingest": False, "image_retention": False, "bind": "127.0.0.1:18765"}
