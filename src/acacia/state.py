"""Timed couch state machine. Pure logic: no model, camera, clock, or I/O.

Time is always an explicit monotonic argument, so behaviour depends on elapsed
seconds and never on frame counts, and tests can drive it deterministically.
The transition table is documented in docs/STATE_MACHINE.md and enforced by
tests/test_state.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from acacia.config import StateConfig


class State(str, Enum):
    UNKNOWN = "UNKNOWN"
    OFF = "OFF"
    CANDIDATE = "CANDIDATE"
    ON = "ON"


class Evidence(str, Enum):
    ON = "ON"                  # a dog is supported by a cushion in this frame
    AMBIGUOUS = "AMBIGUOUS"    # a dog is near or over cushions, support unresolved
    CLEAR_SEEN = "CLEAR_SEEN"  # a confident dog is visibly off the couch
    CLEAR_EMPTY = "CLEAR_EMPTY"  # no dog seen, cushions adequately visible
    OBSTRUCTED = "OBSTRUCTED"  # no dog seen, cushions too hidden to say


@dataclass(frozen=True)
class StateEvent:
    kind: str
    tier: str  # "raw", "confirmed", or "system"
    state_from: State
    state_to: State
    reason: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class _Timer:
    """A sustained-evidence timer that tolerates bounded gaps.

    Elapsed time is measured between actual pieces of evidence, so a timer can
    only complete on a tick that carries evidence, never during a gap.
    """

    first: float | None = None
    last: float | None = None

    def hit(self, t: float, gap: float) -> None:
        if self.first is None or self.last is None or t - self.last > gap:
            self.first = t
        self.last = t

    def expire(self, t: float, gap: float) -> None:
        if self.last is not None and t - self.last > gap:
            self.reset()

    def reset(self) -> None:
        self.first = None
        self.last = None

    @property
    def active(self) -> bool:
        return self.first is not None

    def elapsed(self) -> float:
        if self.first is None or self.last is None:
            return 0.0
        return self.last - self.first


class CouchStateMachine:
    def __init__(self, cfg: StateConfig):
        self.cfg = cfg
        self.state = State.UNKNOWN
        self.reason = "startup"
        self.last_known: State | None = None  # diagnostic only, never a current claim
        self.last_known_at: float | None = None
        # True from any couch-side sighting until a dog is confirmed off the
        # couch. While true, plain absence clears only via the long lost timer.
        self.dog_unaccounted = False
        # A confirmed ON episode stays open through UNKNOWN until clear is
        # confirmed, so one real episode never produces two confirmed-ON events.
        self.episode_open = False
        self.episode_started_at: float | None = None
        self._on = _Timer()
        self._clear = _Timer()
        self._blocked = _Timer()
        self._last_on_evidence: float | None = None
        self._candidate_origin = State.UNKNOWN
        self._faulted = False

    # -- public ------------------------------------------------------------

    def fault(self, t: float, reason: str) -> list[StateEvent]:
        """A health fault: stale stream, failed inference, or invalid calibration.

        Missing evidence is never evidence of absence, so a fault can only move
        the state to UNKNOWN. It cannot clear an episode or the unaccounted flag.
        """
        self._on.reset()
        self._clear.reset()
        self._blocked.reset()
        self._faulted = True
        if self.state is State.UNKNOWN:
            self.reason = reason
            return []
        return [self._to_unknown(t, reason)]

    def observe(self, t: float, evidence: Evidence, visible_fraction: float = 1.0,
                lost_min_visible_fraction: float = 0.95) -> list[StateEvent]:
        """Advance with one fresh, valid observation."""
        events: list[StateEvent] = []
        if self._faulted:
            self._faulted = False
            events.append(StateEvent("recovered", "system", self.state, self.state, self.reason))
            self.reason = "awaiting_evidence"

        gap = self.cfg.evidence_gap_s
        self._on.expire(t, gap)
        self._clear.expire(t, gap)
        self._blocked.expire(t, gap)

        # With a dog possibly hidden, a person anywhere over the cushions blocks
        # clearing even if the cushions are mostly visible.
        if (evidence is Evidence.CLEAR_EMPTY and self.dog_unaccounted
                and visible_fraction < lost_min_visible_fraction):
            evidence = Evidence.OBSTRUCTED

        if evidence is Evidence.ON:
            self.dog_unaccounted = True
            self._last_on_evidence = t
            self._on.hit(t, gap)
            self._clear.reset()
            self._blocked.reset()
        elif evidence is Evidence.CLEAR_SEEN:
            self.dog_unaccounted = False
            self._clear.hit(t, gap)
            self._blocked.reset()
        elif evidence is Evidence.CLEAR_EMPTY:
            self._clear.hit(t, gap)
            self._blocked.reset()
        else:
            if evidence is Evidence.AMBIGUOUS:
                self.dog_unaccounted = True
            self._clear.reset()
            self._blocked.hit(t, gap)

        handler = {
            State.UNKNOWN: self._from_unknown,
            State.OFF: self._from_off,
            State.CANDIDATE: self._from_candidate,
            State.ON: self._from_on,
        }[self.state]
        events.extend(handler(t, evidence))
        return events

    def snapshot(self, t: float) -> dict:
        evidence_age = None if self._last_on_evidence is None else t - self._last_on_evidence
        return {
            "state": self.state.value,
            "reason": self.reason,
            "last_known": self.last_known.value if self.last_known else None,
            "last_known_age_s": None if self.last_known_at is None else round(t - self.last_known_at, 1),
            "dog_unaccounted": self.dog_unaccounted,
            "episode_open": self.episode_open,
            "on_evidence_age_s": None if evidence_age is None else round(evidence_age, 2),
            "on_timer_s": round(self._on.elapsed(), 2),
            "on_confirm_s": self.cfg.on_confirm_s,
            "clear_timer_s": round(self._clear.elapsed(), 2),
            "clear_target_s": self._clear_target(),
        }

    # -- transitions -------------------------------------------------------

    def _clear_target(self) -> float:
        return self.cfg.lost_clear_s if self.dog_unaccounted else self.cfg.clear_confirm_s

    def _clear_due(self) -> bool:
        return self._clear.active and self._clear.elapsed() >= self._clear_target()

    def _set(self, t: float, state: State, reason: str) -> None:
        self.state = state
        self.reason = reason
        if state is not State.UNKNOWN:
            self.last_known = state
            self.last_known_at = t

    def _to_unknown(self, t: float, reason: str) -> StateEvent:
        previous = self.state
        self._set(t, State.UNKNOWN, reason)
        return StateEvent("unknown_entered", "system", previous, State.UNKNOWN, reason)

    def _start_candidate(self, t: float) -> StateEvent:
        previous = self.state
        self._candidate_origin = previous
        self._set(t, State.CANDIDATE, "awaiting_confirmation")
        return StateEvent("candidate_started", "raw", previous, State.CANDIDATE)

    def _confirm_clear(self, t: float, previous: State) -> list[StateEvent]:
        basis = "absence_timeout" if self.dog_unaccounted else "observed_off_couch_or_empty"
        self.dog_unaccounted = False
        self._clear.reset()
        self._set(t, State.OFF, "clear")
        if self.episode_open:
            self.episode_open = False
            duration = None if self.episode_started_at is None else round(t - self.episode_started_at, 1)
            self.episode_started_at = None
            return [StateEvent("confirmed_clear", "confirmed", previous, State.OFF, basis,
                               {"episode_duration_s": duration})]
        return [StateEvent("clear_established", "system", previous, State.OFF, basis)]

    def _from_unknown(self, t: float, ev: Evidence) -> list[StateEvent]:
        if ev is Evidence.ON:
            return [self._start_candidate(t)]
        if self._clear_due():
            return self._confirm_clear(t, State.UNKNOWN)
        if ev is Evidence.OBSTRUCTED:
            self.reason = "occluded_while_on" if self.episode_open else "cushions_obstructed"
        elif ev is Evidence.AMBIGUOUS:
            self.reason = "ambiguous_support"
        elif self.reason in ("cushions_obstructed", "occluded_while_on", "ambiguous_support",
                             "awaiting_evidence", "startup"):
            self.reason = "dog_unaccounted" if self.dog_unaccounted else "confirming_clear"
        return []

    def _from_off(self, t: float, ev: Evidence) -> list[StateEvent]:
        if ev is Evidence.ON:
            return [self._start_candidate(t)]
        if self.dog_unaccounted and self._clear_due():
            self.dog_unaccounted = False  # a passing sighting expires; OFF was already held
        if self._blocked.active and self._blocked.elapsed() >= self.cfg.hold_s:
            reason = "ambiguous_support" if ev is Evidence.AMBIGUOUS else "cushions_obstructed"
            return [self._to_unknown(t, reason)]
        return []

    def _from_candidate(self, t: float, ev: Evidence) -> list[StateEvent]:
        if ev is Evidence.ON:
            if self._on.elapsed() < self.cfg.on_confirm_s:
                return []
            self._set(t, State.ON, "confirmed")
            if self.episode_open:
                return [StateEvent("on_resumed", "system", State.CANDIDATE, State.ON)]
            self.episode_open = True
            self.episode_started_at = self._on.first
            return [StateEvent("confirmed_on", "confirmed", State.CANDIDATE, State.ON,
                                detail={"confirm_delay_s": round(self._on.elapsed(), 2)})]
        if self._on.active:
            return []  # inside the gap tolerance; the candidate survives
        clear_now = ev in (Evidence.CLEAR_SEEN, Evidence.CLEAR_EMPTY)
        if self._candidate_origin is State.OFF and clear_now and not self.episode_open:
            self._set(t, State.OFF, "candidate_cancelled")
            return [StateEvent("candidate_cancelled", "raw", State.CANDIDATE, State.OFF)]
        self._set(t, State.UNKNOWN, "candidate_unresolved")
        return [StateEvent("candidate_cancelled", "raw", State.CANDIDATE, State.UNKNOWN)]

    def _from_on(self, t: float, ev: Evidence) -> list[StateEvent]:
        if ev is Evidence.ON:
            return []
        if self._clear_due():
            return self._confirm_clear(t, State.ON)
        age = t - self._last_on_evidence if self._last_on_evidence is not None else float("inf")
        if age >= self.cfg.hold_s:
            if ev is Evidence.OBSTRUCTED:
                reason = "occluded_while_on"
            elif ev is Evidence.AMBIGUOUS:
                reason = "ambiguous_support"
            else:
                reason = "dog_lost_on_couch"
            return [self._to_unknown(t, reason)]
        return []
