import sqlite3

import pytest

from acacia.events import SCHEMA, EventStore, local_timestamp
from acacia.scoring import (
    Episode,
    build_episodes,
    format_report,
    match_events,
    score_database,
    unknown_seconds,
    wilson_interval,
)


def test_episodes_pair_marks_and_handle_loose_ends():
    episodes, stray = build_episodes(
        [(10, "on_end"), (20, "on_start"), (25, "on_start"), (30, "on_end"), (40, "on_start")], 100)
    assert stray == 1
    assert [(e.start, e.end, e.open_ended) for e in episodes] == [(20, 30, False), (40, 100, True)]


def test_each_episode_counts_once_and_extras_are_duplicates():
    episodes = [Episode(100, 200), Episode(300, 400)]
    matched, duplicates, false_events = match_events(episodes, [105, 150, 305, 500], 5, 10)
    assert (matched, duplicates, false_events) == (2, 1, [500])
    assert episodes[0].event == 105
    assert episodes[0].duplicates == 1


def test_tolerances_bound_a_match():
    assert match_events([Episode(100, 200)], [96], 5, 10)[0] == 1
    assert match_events([Episode(100, 200)], [94], 5, 10)[0] == 0
    assert match_events([Episode(100, 200)], [209], 5, 10)[0] == 1
    assert match_events([Episode(100, 200)], [211], 5, 10)[0] == 0


def test_unknown_seconds_counts_the_opening_interval():
    assert unknown_seconds([(10, "OFF"), (20, "UNKNOWN"), (25, "OFF")], 0, 30) == 15


def test_wilson_interval_stays_wide_for_small_samples():
    low, high = wilson_interval(5, 5)
    assert low < 0.6 and high == pytest.approx(1.0)
    assert wilson_interval(0, 0) is None


def make_db(path, events, truth):
    store = EventStore(path, 1000, 3650, "run")
    store.close()
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    for ts, kind, state in events:
        db.execute("INSERT INTO events (ts_epoch, ts_local, run_id, tier, kind, state_to) VALUES (?,?,?,?,?,?)",
                   (ts, local_timestamp(ts), "run", "confirmed", kind, state))
    for ts, kind in truth:
        db.execute("INSERT INTO truth (ts_epoch, ts_local, run_id, kind) VALUES (?,?,?,?)",
                   (ts, local_timestamp(ts), "run", kind))
    db.commit()
    db.close()


NOW = 1_800_000_000.0


def test_full_report(tmp_path):
    path = tmp_path / "e.sqlite3"
    make_db(path, [
        (NOW, "app_started", None),
        (NOW + 10, "clear_established", "OFF"),
        (NOW + 100, "candidate_started", "CANDIDATE"),
        (NOW + 103, "confirmed_on", "ON"),
        (NOW + 200, "confirmed_clear", "OFF"),
        (NOW + 400, "confirmed_on", "ON"),   # a false event
        (NOW + 401, "confirmed_clear", "OFF"),
        (NOW + 1000, "app_stopped", None),
    ], [(NOW + 99, "on_start"), (NOW + 195, "on_end"), (NOW + 600, "on_start"), (NOW + 700, "on_end")])
    report = score_database(path, None, None, 5, 10)
    assert report.confirmed_events == 2
    assert report.matched_events == 1
    assert len(report.false_events) == 1
    assert report.precision == pytest.approx(0.5)
    assert report.recall == pytest.approx(0.5)
    assert report.raw_candidates == 1
    assert report.unknown_s == pytest.approx(10.0)
    text = format_report(report)
    assert "Missed episodes: 1" in text and "95% CI" in text
    # Raw detection and debounce are reported apart, as the protocol requires.
    assert report.episodes[0].raw_delay_s == pytest.approx(1.0)
    assert report.episodes[0].debounce_s == pytest.approx(3.0)
    assert "Raw detection delay (mark to candidate): n=1, median 1.0 s" in text
    assert "Added debounce delay (candidate to confirmed): n=1, median 3.0 s" in text


def test_zero_denominators_are_undefined(tmp_path):
    path = tmp_path / "e.sqlite3"
    make_db(path, [(NOW, "app_started", None), (NOW + 60, "app_stopped", None)], [])
    report = score_database(path, None, None, 5, 10)
    assert report.precision is None and report.recall is None
    assert format_report(report).count("undefined") == 2


def test_no_data(tmp_path):
    path = tmp_path / "e.sqlite3"
    make_db(path, [], [])
    assert "No events" in format_report(score_database(path, None, None, 5, 10))
