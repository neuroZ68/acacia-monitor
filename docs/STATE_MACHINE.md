# State machine and evidence

The state machine lives in `src/acacia/state.py` and has no dependency on the camera, the model, or the clock. `tests/test_state.py` enforces this table with simulated time. All timer values are hypotheses to validate, not certified numbers.

## 1. Per-frame evidence

Each fresh inference produces one evidence value from the detector boxes and the enabled cushion polygons. A dog's couch support score combines the bottom edge of its box (weight 0.55), the lower 35 percent of its box (0.35), and the whole box (0.10). Whole-box overlap carries little weight because perspective can place a floor dog's box over the cushions, which is why the earlier 60 percent overlap rule was not adopted as a decision rule. The score is independent of detector confidence, and the interface shows both.

| Evidence | Meaning |
|---|---|
| `ON` | A dog box at or above `dog_confidence` has support at or above `on_support` |
| `AMBIGUOUS` | A dog is near or over the cushions but support is unresolved. This covers mid-range support, high box overlap with the footpoint off the cushion, a low-confidence box on a cushion, and a box beside the cushions whose bottom edge is hidden by a person or cut off by the frame |
| `CLEAR_SEEN` | A confident dog is visibly off the couch |
| `CLEAR_EMPTY` | No dog is seen and at least `min_visible_fraction` of the cushion area is unobstructed |
| `OBSTRUCTED` | No dog is seen and people hide too much of the cushions to say |

People are context only. A person can make a frame `OBSTRUCTED` or void a dog's footpoint, but a person never cancels `ON` evidence, so a dog sitting beside someone still counts. When several dogs appear, any `ON` dog decides the frame, then any `AMBIGUOUS` dog, then any off-couch dog. Individuals are not identified. This aggregation assumes a single-dog household in one respect: a dog seen on the floor counts as accounted for, even though a second dog could be hidden.

## 2. Timers and terms

Time is monotonic and elapsed, never a frame count. Wall-clock timestamps with a UTC offset appear only on stored events.

| Setting | Default | Meaning |
|---|---|---|
| `on_confirm_s` | 3.0 | Sustained ON evidence needed to confirm ON |
| `clear_confirm_s` | 5.0 | Sustained clear evidence needed when the dog is accounted for |
| `lost_clear_s` | 60.0 | Sustained clear evidence needed when the dog is unaccounted for |
| `evidence_gap_s` | 1.5 | Longest gap a sustained-evidence timer tolerates |
| `hold_s` | 10.0 | How long ON or OFF may stand without supporting evidence |
| `stale_timeout_s` | 3.0 | Age at which frames or inference count as failed |
| `recovery_warmup_s` | 2.0 | Steady fresh video required after any stream fault |

The dog is **unaccounted for** from any `ON` or `AMBIGUOUS` frame until a `CLEAR_SEEN` frame. This flag is what separates a dog that left from a dog that vanished. A box detector sees a dog under a blanket and an empty cushion identically, so plain absence after a couch-side sighting clears only through the long `lost_clear_s` timer, and only while no person overlaps the cushions (`lost_min_visible_fraction`). That clear event is labelled `absence_timeout`, so it is distinguishable from an observed exit. The clear target below means `clear_confirm_s` when the dog is accounted for and `lost_clear_s` otherwise.

An **episode** opens at `confirmed_on` and closes at `confirmed_clear`. It stays open through UNKNOWN, so one real episode cannot produce two confirmed ON events.

## 3. Transition table

| From | Input | Condition | To | Event (tier) |
|---|---|---|---|---|
| Any | Fault | See section 4 | UNKNOWN | `unknown_entered` (system) |
| UNKNOWN | `ON` | | CANDIDATE | `candidate_started` (raw) |
| UNKNOWN | `CLEAR_SEEN`, `CLEAR_EMPTY` | Clear target sustained | OFF | `confirmed_clear` (confirmed) if an episode is open, otherwise `clear_established` (system) |
| UNKNOWN | `AMBIGUOUS`, `OBSTRUCTED` | | UNKNOWN | None; the clear timer resets |
| OFF | `ON` | | CANDIDATE | `candidate_started` (raw) |
| OFF | `CLEAR_SEEN`, `CLEAR_EMPTY` | | OFF | None |
| OFF | `AMBIGUOUS`, `OBSTRUCTED` | Sustained for `hold_s` | UNKNOWN | `unknown_entered` (system) |
| CANDIDATE | `ON` | Sustained for `on_confirm_s` | ON | `confirmed_on` (confirmed), or `on_resumed` (system) if an episode is open |
| CANDIDATE | Not `ON` | Gap within `evidence_gap_s` | CANDIDATE | None |
| CANDIDATE | `CLEAR_SEEN`, `CLEAR_EMPTY` | Gap exceeded, came from OFF, no open episode | OFF | `candidate_cancelled` (raw) |
| CANDIDATE | Not `ON` | Gap exceeded, otherwise | UNKNOWN | `candidate_cancelled` (raw) |
| ON | `ON` | | ON | None |
| ON | `CLEAR_SEEN`, `CLEAR_EMPTY` | Clear target sustained | OFF | `confirmed_clear` (confirmed) |
| ON | Not `ON` | No ON evidence for `hold_s`, clear not yet due | UNKNOWN | `unknown_entered` (system) |

The first valid observation after any fault also emits `recovered` (system).

## 4. Faults and brief interruptions

A fault means no decision can be trusted, and it can only move the state to UNKNOWN. It never clears an episode or the unaccounted flag. The faults are missing credentials, rejected credentials, no video yet, frames older than `stale_timeout_s`, frames whose stream timeline shows a backlog above `stale_timeout_s`, video still within `recovery_warmup_s` of a fault or reconnect, calibration that is missing, invalid, or unverified, a detector that failed to load, and no successful inference for `stale_timeout_s`.

Brief interruptions are handled in two tiers. A gap of evidence up to `evidence_gap_s` preserves confirmation timers, and elapsed time spans the gap, but a timer can only complete on a tick that carries evidence. A longer gap resets the timer. A fault resets all timers at once. The last known state is retained only as labelled diagnostic context, with its age, and it is never presented as current.

Recovery needs new observations. After a fault, frames must flow steadily for `recovery_warmup_s`, and evidence from before a reconnect is discarded. Arrival time alone cannot identify replayed frames, because a burst after a stall looks new to the local clock, so ingest also compares arrival time with each frame's presentation timestamp. Overall, staleness produces UNKNOWN and never a false all-clear. The first real clip suggested two possible refinements, preserving a candidate through obstruction and limiting the person-overlap rule to where the dog was last seen. Neither is implemented; see section 7 of `docs/RESULTS.md`.
