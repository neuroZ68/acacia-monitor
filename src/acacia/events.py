"""Bounded local metadata store: events, ground-truth marks, resource samples.

Text metadata only. This module has no way to store an image.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts_epoch REAL NOT NULL,
    ts_local TEXT NOT NULL,
    run_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    kind TEXT NOT NULL,
    state_from TEXT,
    state_to TEXT,
    reason TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts_epoch);
CREATE TABLE IF NOT EXISTS truth (
    id INTEGER PRIMARY KEY,
    ts_epoch REAL NOT NULL,
    ts_local TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    ts_epoch REAL NOT NULL,
    ts_local TEXT NOT NULL,
    run_id TEXT NOT NULL,
    data TEXT NOT NULL
);
"""

TRUTH_KINDS = {"on_start", "on_end", "action", "note"}
PRUNE_EVERY = 200


def local_timestamp(epoch: float | None = None) -> str:
    """Wall-clock time with an explicit UTC offset."""
    moment = datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone()
    return moment.isoformat(timespec="milliseconds")


class EventStore:
    def __init__(self, path: Path, max_events: int, max_age_days: int, run_id: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.max_events = max_events
        self.max_age_days = max_age_days
        self.run_id = run_id
        self._lock = threading.Lock()
        self._writes = 0
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        os.chmod(path, 0o600)
        self.prune()

    def add_event(self, tier: str, kind: str, state_from: str | None = None, state_to: str | None = None,
                  reason: str = "", detail: dict | None = None) -> dict:
        epoch = time.time()
        row = {
            "ts_epoch": epoch, "ts_local": local_timestamp(epoch), "run_id": self.run_id, "tier": tier,
            "kind": kind, "state_from": state_from, "state_to": state_to, "reason": reason,
            "detail": json.dumps(detail or {}),
        }
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO events (ts_epoch, ts_local, run_id, tier, kind, state_from, state_to, reason, detail)"
                " VALUES (:ts_epoch, :ts_local, :run_id, :tier, :kind, :state_from, :state_to, :reason, :detail)",
                row)
            self._db.commit()
            row["id"] = cursor.lastrowid
            self._writes += 1
            due = self._writes % PRUNE_EVERY == 0
        if due:
            self.prune()
        return row

    def add_truth(self, kind: str, note: str = "") -> dict:
        if kind not in TRUTH_KINDS:
            raise ValueError(f"unknown truth kind: {kind}")
        epoch = time.time()
        row = {"ts_epoch": epoch, "ts_local": local_timestamp(epoch), "run_id": self.run_id,
               "kind": kind, "note": note[:500]}
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO truth (ts_epoch, ts_local, run_id, kind, note)"
                " VALUES (:ts_epoch, :ts_local, :run_id, :kind, :note)", row)
            self._db.commit()
            row["id"] = cursor.lastrowid
        return row

    def add_metrics(self, data: dict) -> None:
        epoch = time.time()
        with self._lock:
            self._db.execute("INSERT INTO metrics (ts_epoch, ts_local, run_id, data) VALUES (?, ?, ?, ?)",
                             (epoch, local_timestamp(epoch), self.run_id, json.dumps(data)))
            self._db.commit()

    def recent_events(self, limit: int = 200, tier: str | None = None) -> list[dict]:
        query, params = "SELECT * FROM events", []
        if tier:
            query += " WHERE tier = ?"
            params.append(tier)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        with self._lock:
            return [dict(r) for r in self._db.execute(query, params)]

    def recent_truth(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM truth ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    def prune(self) -> None:
        cutoff = time.time() - self.max_age_days * 86400
        with self._lock:
            for table in ("events", "truth", "metrics"):
                self._db.execute(f"DELETE FROM {table} WHERE ts_epoch < ?", (cutoff,))  # noqa: S608
                self._db.execute(
                    f"DELETE FROM {table} WHERE id NOT IN "  # noqa: S608
                    f"(SELECT id FROM {table} ORDER BY id DESC LIMIT ?)", (self.max_events,))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
