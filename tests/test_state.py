"""Transition table tests. Time is simulated; there is no model and no sleeping."""

import random

import pytest

from acacia.config import StateConfig
from acacia.state import CouchStateMachine, Evidence, State

E = Evidence


def run(sm, evidence, start, duration, step=0.25, visible=1.0):
    """Feed the same evidence for `duration` seconds. Returns events and the end time."""
    events = []
    t = start
    while t <= start + duration + 1e-9:
        events += sm.observe(t, evidence, visible)
        t += step
    return events, t


def kinds(events):
    return [e.kind for e in events]


def to_off(sm, t=0.0):
    _, t = run(sm, E.CLEAR_EMPTY, t, 5.0)
    assert sm.state is State.OFF
    return t


def to_on(sm, t):
    _, t = run(sm, E.ON, t, 3.0)
    assert sm.state is State.ON
    return t


@pytest.fixture
def sm(state_cfg):
    return CouchStateMachine(state_cfg)


def test_starts_unknown_and_claims_nothing(sm):
    assert sm.state is State.UNKNOWN
    assert sm.last_known is None


def test_unknown_to_off_needs_sustained_clear(sm):
    run(sm, E.CLEAR_EMPTY, 0.0, 4.5)
    assert sm.state is State.UNKNOWN
    events, _ = run(sm, E.CLEAR_EMPTY, 4.75, 0.5)
    assert sm.state is State.OFF
    assert kinds(events) == ["clear_established"]  # not a confirmed event: no episode existed


def test_off_to_candidate_to_on_at_the_confirm_interval(sm):
    t = to_off(sm)
    events = sm.observe(t, E.ON)
    assert sm.state is State.CANDIDATE and kinds(events) == ["candidate_started"]
    assert events[0].tier == "raw"
    start = t
    while sm.state is State.CANDIDATE:
        t += 0.25
        events = sm.observe(t, E.ON)
    assert kinds(events) == ["confirmed_on"]
    assert events[0].tier == "confirmed"
    assert t - start == pytest.approx(3.0)


@pytest.mark.parametrize("step", [0.05, 0.25, 0.5, 1.0])
def test_confirmation_depends_on_elapsed_time_not_frame_count(state_cfg, step):
    sm = CouchStateMachine(state_cfg)
    t = to_off(sm)
    start = t
    sm.observe(t, E.ON)
    while sm.state is not State.ON:
        t += step
        sm.observe(t, E.ON)
    assert 3.0 <= t - start < 3.0 + step + 1e-9


def test_short_gap_preserves_the_timer_but_long_gap_resets_it(sm):
    t = to_off(sm)
    sm.observe(t, E.ON)
    sm.observe(t + 1.0, E.ON)
    # 1.4 s gap, inside the 1.5 s tolerance: elapsed time keeps counting.
    sm.observe(t + 2.4, E.ON)
    assert sm._on.elapsed() == pytest.approx(2.4)
    # 2 s gap, beyond tolerance: the timer restarts.
    sm.observe(t + 4.4, E.ON)
    assert sm._on.elapsed() == 0.0
    assert sm.state is State.CANDIDATE


def test_confirmation_never_completes_during_a_gap(sm):
    t = to_off(sm)
    sm.observe(t, E.ON)
    sm.observe(t + 2.9, E.AMBIGUOUS)  # no ON evidence: must not confirm
    assert sm.state is not State.ON


def test_candidate_blip_returns_to_off_and_stays_unconfirmed(sm):
    t = to_off(sm)
    sm.observe(t, E.ON)
    events, _ = run(sm, E.CLEAR_EMPTY, t + 0.25, 2.0)
    assert sm.state is State.OFF
    assert kinds(events) == ["candidate_cancelled"]
    assert events[0].tier == "raw"


def test_candidate_from_unknown_never_falls_through_to_off(sm):
    sm.observe(0.0, E.ON)
    assert sm.state is State.CANDIDATE
    events, _ = run(sm, E.CLEAR_EMPTY, 0.25, 2.0)
    assert sm.state is State.UNKNOWN
    assert kinds(events) == ["candidate_cancelled"]


def test_on_clears_quickly_when_the_dog_is_seen_off_the_couch(sm):
    t = to_on(sm, to_off(sm))
    events, _ = run(sm, E.CLEAR_SEEN, t, 5.0)
    assert sm.state is State.OFF
    assert kinds(events) == ["confirmed_clear"]
    assert events[0].tier == "confirmed"
    assert events[0].reason == "observed_off_couch_or_empty"
    assert events[0].detail["episode_duration_s"] > 0


def test_one_off_couch_sighting_then_empty_still_clears_quickly(sm):
    t = to_on(sm, to_off(sm))
    sm.observe(t, E.CLEAR_SEEN)
    events, _ = run(sm, E.CLEAR_EMPTY, t + 0.25, 4.75)
    assert kinds(events) == ["confirmed_clear"]


def test_vanishing_on_the_couch_is_not_a_clear(sm):
    """A dog going under a blanket looks like empty cushions to a box detector."""
    t = to_on(sm, to_off(sm))
    events, t2 = run(sm, E.CLEAR_EMPTY, t, 9.0)
    assert sm.state is State.ON and events == []
    events, t2 = run(sm, E.CLEAR_EMPTY, t2, 1.0)
    assert sm.state is State.UNKNOWN
    assert sm.reason == "dog_lost_on_couch"
    assert kinds(events) == ["unknown_entered"]
    assert sm.episode_open
    # Only the long, clearly labelled absence timeout may end the episode.
    events, _ = run(sm, E.CLEAR_EMPTY, t2, 50.0)
    assert sm.state is State.OFF
    assert kinds(events) == ["confirmed_clear"]
    assert events[0].reason == "absence_timeout"


def test_a_person_over_the_cushions_blocks_clearing_a_lost_dog(sm):
    t = to_on(sm, to_off(sm))
    _, t = run(sm, E.CLEAR_EMPTY, t, 11.0, visible=0.8)
    assert sm.state is State.UNKNOWN
    events, t = run(sm, E.CLEAR_EMPTY, t, 300.0, visible=0.8)
    assert sm.state is State.UNKNOWN and events == []
    assert sm.episode_open


def test_people_do_not_block_clearing_when_the_dog_is_accounted_for(sm):
    t = to_on(sm, to_off(sm))
    sm.observe(t, E.CLEAR_SEEN)
    _, t = run(sm, E.CLEAR_EMPTY, t + 0.25, 5.0, visible=0.8)
    assert sm.state is State.OFF


def test_occlusion_never_clears(sm):
    t = to_on(sm, to_off(sm))
    events, _ = run(sm, E.OBSTRUCTED, t, 600.0)
    assert sm.state is State.UNKNOWN
    assert sm.reason == "occluded_while_on"
    assert "confirmed_clear" not in kinds(events)


def test_fault_goes_to_unknown_and_keeps_last_known_separate(sm):
    t = to_on(sm, to_off(sm))
    events = sm.fault(t, "stale_stream")
    assert sm.state is State.UNKNOWN
    assert kinds(events) == ["unknown_entered"]
    assert sm.last_known is State.ON
    assert sm.snapshot(t + 5)["state"] == "UNKNOWN"
    assert sm.snapshot(t + 5)["last_known_age_s"] == pytest.approx(5.0, abs=0.3)


def test_staleness_is_never_a_false_all_clear(sm):
    t = to_on(sm, to_off(sm))
    events = []
    for i in range(100_000):  # a very long outage
        events += sm.fault(t + i, "stream_disconnected")
    assert sm.state is State.UNKNOWN
    assert "confirmed_clear" not in kinds(events)
    assert sm.episode_open


def test_recovery_to_on_resumes_without_a_duplicate_confirmed_event(sm):
    t = to_on(sm, to_off(sm))
    sm.fault(t, "stale_stream")
    t += 30
    first = sm.observe(t, E.ON)
    assert kinds(first) == ["recovered", "candidate_started"]
    events, _ = run(sm, E.ON, t + 0.25, 3.0)
    assert sm.state is State.ON
    assert kinds(events) == ["on_resumed"]
    assert "confirmed_on" not in kinds(events)


def test_recovery_needs_new_evidence_and_clear_stays_conservative(sm):
    t = to_on(sm, to_off(sm))
    sm.fault(t, "stale_stream")
    # The dog was on the couch when video died, so 5 s of empty cushions is not enough.
    _, t2 = run(sm, E.CLEAR_EMPTY, t + 10, 30.0)
    assert sm.state is State.UNKNOWN
    _, _ = run(sm, E.CLEAR_EMPTY, t2, 31.0)
    assert sm.state is State.OFF


def test_a_fault_resets_timers(sm):
    t = to_off(sm)
    sm.observe(t, E.ON)
    sm.observe(t + 1.0, E.ON)
    sm.fault(t + 1.1, "inference_failed_or_stalled")
    assert sm.snapshot(t + 1.1)["on_timer_s"] == 0.0


def test_off_tolerates_brief_ambiguity_then_admits_uncertainty(sm):
    t = to_off(sm)
    events, t2 = run(sm, E.AMBIGUOUS, t, 9.0)
    assert sm.state is State.OFF and events == []
    events, _ = run(sm, E.AMBIGUOUS, t2, 2.0)
    assert sm.state is State.UNKNOWN
    assert sm.reason == "ambiguous_support"


def test_person_blocking_view_moves_off_to_unknown(sm):
    t = to_off(sm)
    run(sm, E.OBSTRUCTED, t, 11.0)
    assert sm.state is State.UNKNOWN
    assert sm.reason == "cushions_obstructed"


def test_a_passing_sighting_expires_while_off(sm):
    t = to_off(sm)
    sm.observe(t, E.AMBIGUOUS)
    assert sm.dog_unaccounted
    run(sm, E.CLEAR_EMPTY, t + 0.25, 61.0)
    assert sm.state is State.OFF and not sm.dog_unaccounted


def test_unknown_reason_reports_ambiguity(sm):
    sm.observe(0.0, E.AMBIGUOUS)
    assert sm.state is State.UNKNOWN and sm.reason == "ambiguous_support"
    sm.observe(0.25, E.OBSTRUCTED)
    assert sm.reason == "cushions_obstructed"


@pytest.mark.parametrize("seed", range(40))
def test_invariants_hold_under_random_input(seed):
    """Random streams of evidence and faults must never break the event semantics."""
    rng = random.Random(seed)
    cfg = StateConfig()
    sm = CouchStateMachine(cfg)
    t = 0.0
    open_episode = False
    previous_kinds = []
    for _ in range(4000):
        t += rng.choice((0.1, 0.25, 0.5, 2.0))
        if rng.random() < 0.02:
            events = sm.fault(t, "stale_stream")
            assert sm.state is State.UNKNOWN
            assert all(e.kind not in ("confirmed_clear", "confirmed_on") for e in events)
        else:
            run_length = rng.choice((1, 1, 1, 30, 120))
            evidence = rng.choice(list(Evidence))
            visible = rng.choice((1.0, 0.8, 0.3))
            events = []
            for _ in range(run_length):
                t += 0.25
                events += sm.observe(t, evidence, visible)
        for event in events:
            previous_kinds.append(event.kind)
            if event.kind == "confirmed_on":
                assert not open_episode, "two confirmed ON events inside one episode"
                open_episode = True
            if event.kind == "confirmed_clear":
                assert open_episode, "a clear with no episode to close"
                open_episode = False
            if event.kind == "on_resumed":
                assert open_episode
        assert sm.episode_open == open_episode
