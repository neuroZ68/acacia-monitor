import re
import stat
import sqlite3

from acacia.events import EventStore, local_timestamp


def store(tmp_path, max_events=1000, max_age_days=30):
    return EventStore(tmp_path / "e.sqlite3", max_events, max_age_days, "run")


def test_local_timestamps_carry_an_offset():
    assert re.search(r"[+-]\d\d:\d\d$", local_timestamp())


def test_events_are_separated_by_tier(tmp_path):
    s = store(tmp_path)
    s.add_event("raw", "candidate_started")
    s.add_event("confirmed", "confirmed_on", "CANDIDATE", "ON")
    assert [e["kind"] for e in s.recent_events(tier="raw")] == ["candidate_started"]
    assert len(s.recent_events()) == 2
    s.close()


def test_retention_is_bounded_by_count(tmp_path):
    s = store(tmp_path, max_events=50)
    for _ in range(450):
        s.add_event("system", "tick")
    s.prune()
    assert len(s.recent_events(limit=1000)) == 50
    s.close()


def test_retention_is_bounded_by_age(tmp_path):
    s = store(tmp_path)
    s.add_event("system", "old")
    s._db.execute("UPDATE events SET ts_epoch = 1000")
    s._db.commit()
    s.add_event("system", "new")
    s.prune()
    assert [e["kind"] for e in s.recent_events()] == ["new"]
    s.close()


def test_database_is_owner_only_and_stores_no_blobs(tmp_path):
    s = store(tmp_path)
    assert stat.S_IMODE((tmp_path / "e.sqlite3").stat().st_mode) == 0o600
    s.close()
    db = sqlite3.connect(tmp_path / "e.sqlite3")
    types = {row[2].upper() for table in ("events", "truth", "metrics")
             for row in db.execute(f"PRAGMA table_info({table})")}
    assert "BLOB" not in types
    db.close()
