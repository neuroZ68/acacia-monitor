"""Event-level scoring of confirmed ON events against manual ground truth.

Rules, fixed before any trial is scored (see docs/VALIDATION.md):
- A ground-truth episode runs from an on_start mark to the next on_end mark.
- A confirmed_on event matches an episode if it falls inside the episode widened
  by the pre and post tolerances, which absorb hand-timing error.
- Each episode matches once, to its earliest event. Later matching events are
  duplicates. They are not correct matches, so they lower precision.
- Any other confirmed_on event is a false event.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median


@dataclass
class Episode:
    start: float
    end: float
    open_ended: bool = False
    event: float | None = None
    candidate: float | None = None
    duplicates: int = 0

    @property
    def latency_s(self) -> float | None:
        return None if self.event is None else self.event - self.start

    @property
    def raw_delay_s(self) -> float | None:
        return None if self.candidate is None else self.candidate - self.start

    @property
    def debounce_s(self) -> float | None:
        if self.event is None or self.candidate is None:
            return None
        return self.event - self.candidate


@dataclass
class Report:
    episodes: list[Episode]
    confirmed_events: int
    matched_events: int
    duplicate_events: int
    false_events: list[float]
    raw_candidates: int
    raw_cancelled: int
    observation_s: float
    unknown_s: float
    unmatched_end_marks: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        return None if self.confirmed_events == 0 else self.matched_events / self.confirmed_events

    @property
    def recall(self) -> float | None:
        if not self.episodes:
            return None
        return sum(1 for e in self.episodes if e.event is not None) / len(self.episodes)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """95 percent interval that stays honest at the small counts expected here."""
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def build_episodes(marks: list[tuple[float, str]], observation_end: float) -> tuple[list[Episode], int]:
    """Pair on_start and on_end marks. Repeat starts inside an episode are ignored."""
    episodes: list[Episode] = []
    open_start: float | None = None
    stray_ends = 0
    for ts, kind in sorted(marks):
        if kind == "on_start" and open_start is None:
            open_start = ts
        elif kind == "on_end":
            if open_start is None:
                stray_ends += 1
            else:
                episodes.append(Episode(open_start, ts))
                open_start = None
    if open_start is not None:
        episodes.append(Episode(open_start, observation_end, open_ended=True))
    return episodes, stray_ends


def match_events(episodes: list[Episode], events: list[float], pre_s: float, post_s: float) -> tuple[int, int, list[float]]:
    matched = duplicates = 0
    false_events: list[float] = []
    for ts in sorted(events):
        for episode in episodes:
            if episode.start - pre_s <= ts <= episode.end + post_s:
                if episode.event is None:
                    episode.event = ts
                    matched += 1
                else:
                    episode.duplicates += 1
                    duplicates += 1
                break
        else:
            false_events.append(ts)
    return matched, duplicates, false_events


def attach_candidates(episodes: list[Episode], candidates: list[float], pre_s: float, post_s: float) -> None:
    """Record the first raw candidate inside each episode, for separate delay reporting."""
    for episode in episodes:
        inside = [ts for ts in candidates if episode.start - pre_s <= ts <= episode.end + post_s]
        episode.candidate = min(inside) if inside else None


def unknown_seconds(transitions: list[tuple[float, str]], start: float, end: float) -> float:
    """Total time in UNKNOWN between start and end, given (time, new state) pairs."""
    total = 0.0
    state, since = "UNKNOWN", start
    for ts, new_state in sorted(transitions):
        if ts < start or ts > end:
            continue
        if state == "UNKNOWN":
            total += ts - since
        state, since = new_state, ts
    if state == "UNKNOWN":
        total += end - since
    return total


def score_database(path: Path, since: float | None, until: float | None, pre_s: float, post_s: float) -> Report:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    low, high = since or 0.0, until or float("inf")

    events = [r for r in db.execute("SELECT * FROM events ORDER BY ts_epoch") if low <= r["ts_epoch"] <= high]
    marks = [(r["ts_epoch"], r["kind"]) for r in db.execute("SELECT * FROM truth ORDER BY ts_epoch")
             if low <= r["ts_epoch"] <= high and r["kind"] in ("on_start", "on_end")]
    db.close()

    if not events:
        return Report([], 0, 0, 0, [], 0, 0, 0.0, 0.0, notes=["No events in the selected window."])

    start, end = events[0]["ts_epoch"], events[-1]["ts_epoch"]
    if marks:
        start, end = min(start, marks[0][0]), max(end, marks[-1][0])
    episodes, stray = build_episodes(marks, end)
    confirmed = [r["ts_epoch"] for r in events if r["kind"] == "confirmed_on"]
    matched, duplicates, false_events = match_events(episodes, confirmed, pre_s, post_s)
    attach_candidates(episodes, [r["ts_epoch"] for r in events if r["kind"] == "candidate_started"], pre_s, post_s)
    transitions = [(r["ts_epoch"], r["state_to"]) for r in events if r["state_to"]]

    notes = []
    if any(e.open_ended for e in episodes):
        notes.append("An episode has no on_end mark; it was closed at the end of the window.")
    if stray:
        notes.append(f"{stray} on_end mark(s) had no matching on_start and were ignored.")
    return Report(
        episodes=episodes,
        confirmed_events=len(confirmed),
        matched_events=matched,
        duplicate_events=duplicates,
        false_events=false_events,
        raw_candidates=sum(1 for r in events if r["kind"] == "candidate_started"),
        raw_cancelled=sum(1 for r in events if r["kind"] == "candidate_cancelled"),
        observation_s=end - start,
        unknown_s=unknown_seconds(transitions, start, end),
        unmatched_end_marks=stray,
        notes=notes,
    )


def _percent(value: float | None) -> str:
    return "undefined (zero denominator)" if value is None else f"{value * 100:.1f}%"


def _interval(pair: tuple[float, float] | None) -> str:
    return "" if pair is None else f"  [95% CI {pair[0] * 100:.0f} to {pair[1] * 100:.0f}%]"


def _delay_line(label: str, values: list[float | None]) -> str:
    samples = sorted(v for v in values if v is not None)
    if not samples:
        return ""
    line = f"{label}: n={len(samples)}, median {median(samples):.1f} s, range {samples[0]:.1f} to {samples[-1]:.1f} s"
    # An upper percentile from a few samples would be false precision.
    if len(samples) >= 20:
        line += f", p95 {samples[int(len(samples) * 0.95)]:.1f} s"
    return line


def format_report(report: Report) -> str:
    detected = sum(1 for e in report.episodes if e.event is not None)
    hours = report.observation_s / 3600
    unknown_share = report.unknown_s / report.observation_s * 100 if report.observation_s else 0.0
    lines = [
        f"Observation: {report.observation_s:.0f} s ({hours:.2f} h)",
        f"UNKNOWN: {report.unknown_s:.0f} s ({unknown_share:.1f}% of observation; not excluded from anything below)",
        f"Ground-truth ON episodes: {len(report.episodes)}",
        f"Confirmed ON events: {report.confirmed_events}",
        f"  matched {report.matched_events}, duplicates {report.duplicate_events}, false {len(report.false_events)}",
        f"Missed episodes: {len(report.episodes) - detected}",
        f"Precision: {_percent(report.precision)}{_interval(wilson_interval(report.matched_events, report.confirmed_events))}",
        f"Recall: {_percent(report.recall)}{_interval(wilson_interval(detected, len(report.episodes)))}",
        f"Raw candidates: {report.raw_candidates} started, {report.raw_cancelled} cancelled",
    ]
    if hours > 0:
        lines.append(f"False confirmed ON per hour: {len(report.false_events) / hours:.2f}")
    lines.extend(filter(None, [
        _delay_line("Raw detection delay (mark to candidate)", [e.raw_delay_s for e in report.episodes]),
        _delay_line("Added debounce delay (candidate to confirmed)", [e.debounce_s for e in report.episodes]),
        _delay_line("Total delay (mark to confirmed)", [e.latency_s for e in report.episodes]),
    ]))
    lines.append("Delays that start at a mark include hand-timing error, and a candidate may precede its mark.")
    lines.extend(f"Note: {note}" for note in report.notes)
    return "\n".join(lines)
