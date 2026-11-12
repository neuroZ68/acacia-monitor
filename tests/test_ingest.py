import threading
import time

import numpy as np
from conftest import INFO, FakeSource, wait_for

from acacia.config import CameraConfig
from acacia.credentials import CredentialError, RtspUrl
from acacia.ingest import AuthFailed, BacklogTracker, Frame, IngestStatus, IngestWorker, LatestSlot


def url():
    return RtspUrl("192.0.2.1", 554, "/stream2", None)


def worker(factory, provider=url, **cam):
    slot, status = LatestSlot(), IngestStatus()
    config = CameraConfig(host="192.0.2.1", reconnect_max_backoff_s=0.2, **cam)
    thread = IngestWorker(config, provider, slot, status, lambda frame: None, factory)
    return thread, slot, status


def test_slot_keeps_only_the_newest_item_however_far_behind_the_consumer_is():
    slot = LatestSlot()
    for i in range(100_000):
        slot.put(i)
    item, version = slot.take_newer(0, 0)
    assert item == 99_999
    assert version == 100_000
    assert slot.overwritten == 99_999


def test_slot_counts_only_frames_nobody_consumed():
    slot = LatestSlot()
    slot.put("a")
    _, version = slot.take_newer(0, 0)
    slot.put("b")
    assert slot.overwritten == 0
    slot.put("c")
    assert slot.overwritten == 1
    assert slot.take_newer(version, 0)[0] == "c"


def test_take_newer_times_out_rather_than_repeating_old_work():
    slot = LatestSlot()
    slot.put("a")
    _, version = slot.take_newer(0, 0)
    started = time.monotonic()
    assert slot.take_newer(version, 0.05) == (None, version)
    assert 0.03 < time.monotonic() - started < 0.5


def test_slot_wakes_a_waiter():
    slot = LatestSlot()
    threading.Timer(0.02, slot.put, ["x"]).start()
    assert slot.take_newer(0, 1.0)[0] == "x"


def test_backlog_is_zero_for_a_steady_stream():
    tracker = BacklogTracker()
    assert all(tracker.update(100 + i / 15, i / 15) < 0.001 for i in range(300))


def test_backlog_exposes_replayed_frames_that_look_new_on_arrival():
    tracker = BacklogTracker()
    for i in range(150):
        tracker.update(100 + i / 15, i / 15)
    # The network stalls for 5 s, then a burst of old frames arrives at once.
    backlogs = [tracker.update(115 + i * 0.001, 10 + i / 15) for i in range(30)]
    assert backlogs[0] > 4.0  # each of these arrived "just now" by the local clock
    assert backlogs[-1] < backlogs[0]


def test_backlog_absorbs_clock_drift():
    tracker = BacklogTracker()
    # The camera clock runs 50 ppm slow for about 3 hours.
    values = [tracker.update(i * 1.00005, float(i)) for i in range(10_000)]
    assert values[-1] < 0.5


def test_backlog_survives_a_timeline_restart():
    tracker = BacklogTracker()
    tracker.update(100.0, 0.0)
    assert tracker.update(101.0, 5000.0) == 0.0


def test_backlog_without_timestamps():
    assert BacklogTracker().update(1.0, None) == 0.0


def test_frame_conversion_is_lazy_and_rotation_swaps_dimensions():
    pixels = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
    frame = Frame(1, 1, pixels, 3, 2, 0.0, 0.0, rotation_deg=90)
    assert (frame.width, frame.height) == (2, 3)
    assert frame.rgb().shape == (3, 2, 3)
    assert frame.rgb()[0, 1].tolist() == pixels[0, 0].tolist()


def test_concurrent_pixel_access_is_safe():
    class SlowSource:
        def to_ndarray(self, format):
            time.sleep(0.01)
            return np.zeros((2, 3, 3), dtype=np.uint8)

    frame = Frame(1, 1, SlowSource(), 3, 2, 0.0, 0.0)
    results, errors = [], []

    def read():
        try:
            results.append(frame.rgb())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert all(r is results[0] for r in results)


def test_worker_delivers_frames_and_stops_quickly():
    source = FakeSource(fps=100)
    thread, slot, status = worker(lambda u, c: source)
    thread.start()
    wait_for(lambda: slot.peek()[0] is not None, message="first frame")
    started = time.monotonic()
    thread.stop()
    thread.join(2)
    assert not thread.is_alive()
    assert time.monotonic() - started < 1.0
    assert source.closed
    assert status.phase == "stopped"


def test_worker_reconnects_after_the_stream_ends():
    sources = []

    def factory(u, c):
        sources.append(FakeSource(fps=200, frames=3))
        return sources[-1]

    thread, _, status = worker(factory)
    thread.start()
    wait_for(lambda: status.reconnects >= 3, message="reconnects")
    thread.stop()
    thread.join(2)
    assert all(s.closed for s in sources)
    assert status.session >= 3


def test_backoff_grows_and_is_capped():
    def factory(u, c):
        raise OSError("unreachable")

    sleeps = []
    slot, status = LatestSlot(), IngestStatus()
    config = CameraConfig(host="192.0.2.1", reconnect_max_backoff_s=8.0)
    thread = IngestWorker(config, url, slot, status, lambda f: None, factory)
    thread._sleep = sleeps.append  # record instead of waiting
    thread.start()
    wait_for(lambda: len(sleeps) >= 8, message="retries")
    thread.stop()
    thread.join(2)
    assert sleeps[0] <= 1.2
    assert sleeps[3] >= 6.4  # grew 1, 2, 4, 8 with at most 20 percent jitter
    assert max(sleeps) <= 8.0  # the cap holds even after jitter


def test_stop_cancels_a_pending_backoff():
    def factory(u, c):
        raise OSError("unreachable")

    slot, status = LatestSlot(), IngestStatus()
    config = CameraConfig(host="192.0.2.1", reconnect_max_backoff_s=60)
    thread = IngestWorker(config, url, slot, status, lambda f: None, factory)
    thread.start()
    wait_for(lambda: status.phase == "reconnecting", message="retry wait")
    started = time.monotonic()
    thread.stop()
    thread.join(2)
    assert not thread.is_alive()
    assert time.monotonic() - started < 0.5


def test_auth_failure_pauses_instead_of_hammering_the_camera():
    attempts = []

    def factory(u, c):
        attempts.append(1)
        raise AuthFailed()

    thread, _, status = worker(factory, auth_max_attempts=3)
    thread.start()
    wait_for(lambda: status.phase == "auth_failed", message="auth pause")
    time.sleep(0.3)
    assert len(attempts) == 3
    thread.retry()  # the UI Retry button
    wait_for(lambda: len(attempts) > 3, message="manual retry")
    thread.stop()
    thread.join(2)
    assert not thread.is_alive()


def test_stop_releases_an_auth_pause():
    def factory(u, c):
        raise AuthFailed()

    thread, _, status = worker(factory, auth_max_attempts=1)
    thread.start()
    wait_for(lambda: status.phase == "auth_failed", message="auth pause")
    thread.stop()
    thread.join(2)
    assert not thread.is_alive()


def test_missing_credentials_are_reported_without_a_crash():
    def provider():
        raise CredentialError("No camera credentials in the Keychain.")

    thread, _, status = worker(lambda u, c: FakeSource(), provider)
    thread.start()
    wait_for(lambda: status.phase == "no_credentials", message="credential status")
    assert "Keychain" in status.last_error
    thread.stop()
    thread.join(2)
    assert not thread.is_alive()


def test_ingest_status_reports_audio_track_count():
    status = IngestStatus()
    status.set(info=INFO)
    assert status.to_json()["stream"]["audio_tracks_opened"] == 0


def test_fake_camera_keeps_its_own_clock_when_sleeps_overshoot(monkeypatch):
    # A busy CI runner oversleeps. If the fake camera's timeline drifted with it,
    # the pipeline tests would see growing backlog and go UNKNOWN for that reason.
    import conftest

    real_sleep = time.sleep
    monkeypatch.setattr(conftest.time, "sleep", lambda seconds: real_sleep(seconds + 0.005))
    source = FakeSource(fps=60, frames=60)
    tracker = BacklogTracker()
    worst = 0.0
    for _pixels, _w, _h, pts in source.frames():
        worst = max(worst, tracker.update(time.monotonic(), pts))
    assert worst < 0.1
