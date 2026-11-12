"""Optional voice cue, sent to the private Alexa skill when ON is confirmed.

This is the only part of the application that can act on an animal, and it was
added at Zimu's request after phase 1. It is disarmed at every start and must be
armed by hand. While disarmed it only records what it would have done, so its
behaviour can be reviewed before Acacia ever hears anything.

A late cue is worse than none, so a failed send is logged and never retried.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as clock_time
from typing import Protocol

from acacia.config import CueConfig
from acacia.sanitize import scrub

log = logging.getLogger("acacia.cue")

SEND_TIMEOUT_S = 8.0


class CueSendError(Exception):
    pass


class Sender(Protocol):
    def send(self, action: str) -> None:
        ...


class HttpSender:
    """Posts a signed request to the Lambda Function URL."""

    def __init__(self, endpoint: str, secret_provider: Callable[[], str]):
        self.endpoint = endpoint
        self.secret_provider = secret_provider

    def request(self, action: str) -> dict:
        body = json.dumps({"action": action, "ts": time.time()}).encode()
        signature = hmac.new(self.secret_provider().encode(), body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Acacia-Signature": signature})
        try:
            with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_S) as reply:
                return json.loads(reply.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise CueSendError(f"skill endpoint answered {exc.code}: {scrub(exc.read()[:200])}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise CueSendError(f"could not reach the skill endpoint: {type(exc).__name__}") from None

    def send(self, action: str) -> None:
        self.request(action)


@dataclass(frozen=True)
class Decision:
    play: bool
    reason: str


def parse_clock(value: str) -> clock_time:
    return datetime.strptime(value, "%H:%M").time()


def in_quiet_hours(moment: clock_time, start: clock_time, end: clock_time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end  # the window crosses midnight


def evaluate(cfg: CueConfig, now: float, local: datetime, last_sent: float | None, sent_today: int,
             last_person: float | None) -> Decision:
    """Pure gating logic: would a cue be allowed right now?"""
    if in_quiet_hours(local.time(), parse_clock(cfg.quiet_start), parse_clock(cfg.quiet_end)):
        return Decision(False, "quiet_hours")
    if cfg.silent_when_people and last_person is not None and now - last_person < cfg.people_clear_s:
        return Decision(False, "people_in_view")
    if last_sent is not None and now - last_sent < cfg.cooldown_s:
        return Decision(False, "cooldown")
    if sent_today >= cfg.max_per_day:
        return Decision(False, "daily_limit")
    return Decision(True, "ok")


class CueController:
    def __init__(self, cfg: CueConfig, record: Callable[[str, str, dict], None], sender: Sender | None):
        self.cfg = cfg
        self.record = record  # (kind, reason, detail)
        self.sender = sender
        self.armed = False  # deliberately not persisted: every start is disarmed
        self._lock = threading.Lock()
        self._last_sent: float | None = None
        self._day = None
        self._sent_today = 0
        self._last_decision = ""
        self._queue: queue.Queue[str] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="cue", daemon=True)

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(SEND_TIMEOUT_S + 1)

    @property
    def can_arm(self) -> bool:
        return self.cfg.enabled and self.sender is not None

    def set_armed(self, armed: bool) -> bool:
        if armed and not self.can_arm:
            raise ValueError("The cue is not configured. See alexa/README.md")
        with self._lock:
            changed = armed != self.armed
            self.armed = armed
        if changed:
            self.record("cue_armed" if armed else "cue_disarmed", "", {})
        return self.armed

    def on_confirmed_on(self, now: float, local: datetime, last_person: float | None) -> None:
        """Called once per confirmed visit. Resumed episodes never reach this."""
        with self._lock:
            if self._day != local.date():
                self._day, self._sent_today = local.date(), 0
            decision = evaluate(self.cfg, now, local, self._last_sent, self._sent_today, last_person)
            self._last_decision = decision.reason
            armed = self.armed
            if decision.play and armed:
                self._last_sent = now
                self._sent_today += 1

        if not armed:
            kind = "cue_would_play" if decision.play else "cue_would_skip"
            self.record(kind, decision.reason, {})
        elif not decision.play:
            self.record("cue_skipped", decision.reason, {})
        else:
            try:
                self._queue.put_nowait("detected")
            except queue.Full:
                self.record("cue_skipped", "send_in_progress", {})

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            started = time.monotonic()
            try:
                self.sender.send("detected")
            except Exception as exc:  # noqa: BLE001 - a cue failure must never touch detection
                message = scrub(exc)
                self.record("cue_failed", message, {})
                log.warning("cue failed: %s", message)
                continue
            self.record("cue_sent", "", {"send_ms": round((time.monotonic() - started) * 1e3)})
            # The sensor must return to clear before it can fire another routine.
            if self._stop.wait(self.cfg.clear_after_s):
                continue
            try:
                self.sender.send("clear")
            except Exception as exc:  # noqa: BLE001
                self.record("cue_clear_failed", scrub(exc), {})

    def status(self) -> dict:
        with self._lock:
            return {
                "armed": self.armed,
                "can_arm": self.can_arm,
                "sent_today": self._sent_today,
                "max_per_day": self.cfg.max_per_day,
                "cooldown_s": self.cfg.cooldown_s,
                "quiet_hours": f"{self.cfg.quiet_start} to {self.cfg.quiet_end}",
                "silent_when_people": self.cfg.silent_when_people,
                "last_decision": self._last_decision,
            }
