"""Voice cue gating and safety. A fake sender stands in for the network."""

import time
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from conftest import FakeSource, ScriptedDetector, dog, person, wait_for

from acacia.config import Config, ConfigError, CueConfig, validate
from acacia.cue import CueController, CueSendError, HttpSender, evaluate, in_quiet_hours, parse_clock
from acacia.pipeline import Pipeline

NOON = datetime(2026, 11, 8, 12, 0)
LATE = datetime(2026, 11, 8, 23, 30)
EARLY = datetime(2026, 11, 8, 6, 30)
CFG = CueConfig(enabled=True, endpoint="https://example.invalid/", clear_after_s=30)
FAST = replace(CFG, clear_after_s=0.05)


class FakeSender:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def send(self, action):
        self.calls.append(action)
        if self.error:
            raise self.error


class Harness:
    def __init__(self, cfg=FAST, sender=None):
        self.events = []
        self.sender = sender if sender is not None else FakeSender()
        self.cue = CueController(cfg, lambda kind, reason, detail: self.events.append((kind, reason)), self.sender)
        self.cue.start()

    def kinds(self):
        return [kind for kind, _ in self.events]

    def close(self):
        self.cue.stop()


@pytest.fixture
def harness():
    h = Harness()
    yield h
    h.close()


# -- gating ------------------------------------------------------------------


def test_allowed_at_noon_with_nobody_around():
    assert evaluate(CFG, 1000.0, NOON, None, 0, None).play


@pytest.mark.parametrize("moment, quiet", [(LATE, True), (EARLY, True), (NOON, False)])
def test_quiet_hours_cross_midnight(moment, quiet):
    decision = evaluate(CFG, 1000.0, moment, None, 0, None)
    assert decision.play is not quiet
    assert (decision.reason == "quiet_hours") is quiet


def test_quiet_window_edges():
    start, end = parse_clock("22:00"), parse_clock("07:00")
    assert in_quiet_hours(parse_clock("22:00"), start, end)
    assert not in_quiet_hours(parse_clock("07:00"), start, end)
    assert not in_quiet_hours(parse_clock("12:00"), parse_clock("09:00"), parse_clock("09:00"))


def test_silent_when_a_person_was_seen_recently():
    assert evaluate(CFG, 1000.0, NOON, None, 0, 990.0).reason == "people_in_view"
    assert evaluate(CFG, 1000.0, NOON, None, 0, 960.0).play


def test_people_rule_can_be_turned_off():
    cfg = replace(CFG, silent_when_people=False)
    assert evaluate(cfg, 1000.0, NOON, None, 0, 999.0).play


def test_cooldown_and_daily_limit():
    assert evaluate(CFG, 1000.0, NOON, 900.0, 1, None).reason == "cooldown"
    assert evaluate(CFG, 2000.0, NOON, 900.0, 6, None).reason == "daily_limit"


# -- arming -------------------------------------------------------------------


def test_always_starts_disarmed(harness):
    assert harness.cue.armed is False


def test_disarmed_never_sends_and_logs_what_it_would_do(harness):
    harness.cue.on_confirmed_on(1000.0, NOON, None)
    harness.cue.on_confirmed_on(2000.0, LATE, None)
    time.sleep(0.1)
    assert harness.sender.calls == []
    assert harness.events == [("cue_would_play", "ok"), ("cue_would_skip", "quiet_hours")]


def test_arming_needs_config_and_a_sender():
    with pytest.raises(ValueError, match="not configured"):
        CueController(CueConfig(), lambda *a: None, FakeSender()).set_armed(True)
    with pytest.raises(ValueError, match="not configured"):
        CueController(CFG, lambda *a: None, None).set_armed(True)


def test_armed_sends_then_resets_the_sensor(harness):
    harness.cue.set_armed(True)
    harness.cue.on_confirmed_on(1000.0, NOON, None)
    wait_for(lambda: harness.sender.calls == ["detected", "clear"], message="send and reset")
    assert harness.kinds() == ["cue_armed", "cue_sent"]
    assert harness.cue.status()["sent_today"] == 1


def test_armed_still_obeys_every_gate(harness):
    harness.cue.set_armed(True)
    harness.cue.on_confirmed_on(1000.0, LATE, None)
    harness.cue.on_confirmed_on(1000.0, NOON, 999.0)
    time.sleep(0.1)
    assert harness.sender.calls == []
    assert harness.events[1:] == [("cue_skipped", "quiet_hours"), ("cue_skipped", "people_in_view")]


def test_cooldown_applies_between_visits(harness):
    harness.cue.set_armed(True)
    harness.cue.on_confirmed_on(1000.0, NOON, None)
    wait_for(lambda: "cue_sent" in harness.kinds(), message="first play")
    harness.cue.on_confirmed_on(1100.0, NOON, None)
    assert ("cue_skipped", "cooldown") in harness.events


def test_daily_count_resets_at_local_midnight():
    h = Harness(replace(FAST, max_per_day=1, cooldown_s=0))
    h.cue.set_armed(True)
    h.cue.on_confirmed_on(1000.0, NOON, None)
    wait_for(lambda: h.sender.calls == ["detected", "clear"], message="first play")
    h.cue.on_confirmed_on(2000.0, NOON, None)
    assert ("cue_skipped", "daily_limit") in h.events
    h.cue.on_confirmed_on(3000.0, NOON + timedelta(days=1), None)
    wait_for(lambda: h.sender.calls.count("detected") == 2, message="next day")
    h.close()


def test_a_failed_send_is_logged_once_and_never_retried():
    h = Harness(sender=FakeSender(CueSendError("skill endpoint answered 502")))
    h.cue.set_armed(True)
    h.cue.on_confirmed_on(1000.0, NOON, None)
    wait_for(lambda: "cue_failed" in h.kinds(), message="failure event")
    time.sleep(0.15)
    assert h.sender.calls == ["detected"]
    h.close()


def test_stop_cancels_a_pending_reset():
    h = Harness(replace(CFG, clear_after_s=60))
    h.cue.set_armed(True)
    h.cue.on_confirmed_on(1000.0, NOON, None)
    wait_for(lambda: h.sender.calls == ["detected"], message="send")
    started = time.monotonic()
    h.close()
    assert time.monotonic() - started < 1.0


# -- signing ------------------------------------------------------------------


def test_signature_covers_the_exact_body(monkeypatch):
    import hashlib
    import hmac
    import io
    import json

    captured = {}

    def fake_open(request, timeout):
        captured["body"], captured["headers"] = request.data, dict(request.headers)
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr("acacia.cue.urllib.request.urlopen", fake_open)
    assert HttpSender("https://example.invalid/", lambda: "s3cret").request("ping") == {"ok": True}
    expected = hmac.new(b"s3cret", captured["body"], hashlib.sha256).hexdigest()
    assert captured["headers"]["X-acacia-signature"] == expected
    assert "s3cret" not in json.dumps(json.loads(captured["body"]))


# -- config -------------------------------------------------------------------


def with_cue(**values):
    cfg = Config()
    return replace(cfg, camera=replace(cfg.camera, host="192.0.2.1"), cue=replace(cfg.cue, **values))


def test_cue_is_off_by_default():
    assert Config().cue.enabled is False


@pytest.mark.parametrize("values, message", [
    ({"enabled": True, "endpoint": "http://example.invalid/"}, "https"),
    ({"enabled": True}, "https"),
    ({"endpoint": "https://example.invalid/?token=abc"}, "plain URL"),
    ({"clear_after_s": 10.0}, "at least 30"),
    ({"quiet_start": "25:00"}, "quiet_start"),
])
def test_bad_cue_config_is_refused(values, message):
    with pytest.raises(ConfigError, match=message):
        validate(with_cue(**values))


# -- end to end with the real threads and state machine ------------------------

ON_BOX = (0.45, 0.25, 0.55, 0.58)
FLOOR_BOX = (0.30, 0.75, 0.42, 0.95)
SEAT = [[0.2, 0.4], [0.9, 0.4], [0.9, 0.62], [0.2, 0.62]]


def build(fast_config, paths, sender):
    cue = replace(CFG, quiet_start="00:00", quiet_end="00:00", cooldown_s=0.0, clear_after_s=30.0)
    detector = ScriptedDetector()
    pipeline = Pipeline(replace(fast_config, cue=cue), paths, lambda c: detector,
                        source_factory=lambda u, c: FakeSource(fps=60), cue_sender=sender)
    pipeline._url = lambda: None
    return pipeline, detector


def calibrate(pipeline):
    wait_for(lambda: pipeline._live_view is not None, message="live view")
    pipeline.save_polygons([{"name": "seat", "points": SEAT}])
    wait_for(lambda: pipeline.machine.state.value == "OFF", message="OFF")


def cue_kinds(pipeline):
    return [e["kind"] for e in reversed(pipeline.store.recent_events(tier="cue"))]


def test_disarmed_pipeline_only_logs(fast_config, paths):
    sender = FakeSender()
    pipeline, detector = build(fast_config, paths, sender)
    pipeline.start()
    try:
        calibrate(pipeline)
        detector.detections = [dog(ON_BOX)]
        wait_for(lambda: "cue_would_play" in cue_kinds(pipeline), message="shadow event")
        assert sender.calls == []
    finally:
        pipeline.stop()


def test_armed_pipeline_plays_once_per_visit(fast_config, paths):
    sender = FakeSender()
    pipeline, detector = build(fast_config, paths, sender)
    pipeline.start()
    try:
        calibrate(pipeline)
        pipeline.cue.set_armed(True)
        detector.detections = [dog(ON_BOX)]
        wait_for(lambda: sender.calls == ["detected"], message="play")
        time.sleep(0.4)  # she stays on the couch: no repeat
        assert sender.calls == ["detected"]
    finally:
        pipeline.stop()


def test_people_keep_the_cue_silent(fast_config, paths):
    sender = FakeSender()
    pipeline, detector = build(fast_config, paths, sender)
    pipeline.start()
    try:
        calibrate(pipeline)
        pipeline.cue.set_armed(True)
        detector.detections = [dog(ON_BOX), person((0.75, 0.3, 0.92, 0.9))]
        wait_for(lambda: "cue_skipped" in cue_kinds(pipeline), message="skip")
        assert sender.calls == []
    finally:
        pipeline.stop()


def test_a_cue_failure_cannot_disturb_detection(fast_config, paths):
    sender = FakeSender(RuntimeError("network exploded"))
    pipeline, detector = build(fast_config, paths, sender)
    pipeline.start()
    try:
        calibrate(pipeline)
        pipeline.cue.set_armed(True)
        detector.detections = [dog(ON_BOX)]
        wait_for(lambda: "cue_failed" in cue_kinds(pipeline), message="failure")
        assert pipeline.machine.state.value == "ON"
        detector.detections = [dog(FLOOR_BOX)]
        wait_for(lambda: pipeline.machine.state.value == "OFF", message="clear still works")
    finally:
        pipeline.stop()


def test_restart_comes_back_disarmed(fast_config, paths):
    first, _ = build(fast_config, paths, FakeSender())
    first.start()
    first.cue.set_armed(True)
    first.stop()
    second, _ = build(fast_config, paths, FakeSender())
    second.start()
    try:
        assert second.cue.armed is False
    finally:
        second.stop()
