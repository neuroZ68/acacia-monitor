# Validation protocol

This protocol measures event-level accuracy and delay on the real camera. It uses supervised natural behaviour only. Do not provoke, restrain, lure, or force repeated couch trials, and pause if Acacia appears distressed. Ordinary opportunities are enough, so expect the matrix to fill over days rather than in one sitting.

## 1. Before scoring

Three things must be fixed first, because changing them afterwards would invalidate the counts. Record the model, the config values shown in the Settings tab, the camera placement, and the lighting. Keep the ground-truth definition below unchanged. Agree the event matching rules below.

The agreed ON definition is that the dog's body is supported by a cushion. This includes standing or walking on the cushions as well as sitting or lying, following the handoff's goal of a dog physically on the cushions, so mark such moments as ON. Zimu settled the paws-only case on 2026-11-07: a dog standing on the floor with only its front paws on the couch is **not ON**. Do not press **Dog got ON** for it, and add a note instead if it is worth reviewing. The classifier treats this posture as `AMBIGUOUS`, which can never confirm ON, so the definition and the code agree.

## 2. Recording ground truth

Open the Validate tab and press **Dog got ON** and **Dog got OFF** as things happen, adding a short note on posture, lighting, and people. Each press stores only a timestamp and the note. If a press was late, say so in a note instead of editing the data. Unsure cases are worth a note too, because reviewing them is how the ambiguous band gets tuned.

No images are kept by default. Saved clips would make review easier, but they need an agreed purpose and retention policy with Zimu first, and this application has no capture feature.

Saved clips can be replayed through the real detector, classifier, and state machine, with video timestamps as the clock.

```bash
./acacia replay path/to/clip.mp4
```

Replay reads video only and writes nothing, so it adds no retention of its own, and it gives repeatable results for rerunning scenarios after tuning. Clips stay wherever Zimu keeps them; the repository ignores video files. Polygons only apply to a clip if the camera has not moved since it was recorded, and a clip scored this way should be reported as replayed rather than live.

## 3. Event matching

A ground-truth episode runs from an `on_start` mark to the next `on_end` mark, and each continuous episode counts once. A `confirmed_on` event matches an episode if it falls between 5 seconds before the start and 10 seconds after the end; these tolerances absorb hand-timing error and can be changed with `--pre` and `--post`, but they must be fixed before scoring. Each episode matches its earliest event. Later events inside the same episode are duplicates, and they lower precision because they are not correct matches. Any other `confirmed_on` event is a false event.

Precision is correctly matched confirmed ON events divided by all emitted confirmed ON events. Recall is detected ground-truth episodes divided by all ground-truth episodes. A zero denominator is reported as undefined. UNKNOWN intervals are reported and never excluded, so an episode missed during UNKNOWN still counts as a miss. Raw candidate behaviour is reported separately from confirmed events.

```bash
./acacia score --since 2026-11-08T09:00 --until 2026-11-08T21:00
```

The report includes 95 percent Wilson intervals because the counts here will be small. Five successes out of five gives an interval that reaches down to about 57 percent, which is why a few staged successes cannot establish a percentage.

## 4. Scenario matrix

Exercise each case in daylight and in infrared night view where practical, and record results in `docs/RESULTS.md`.

| ID | Scenario | Expected behaviour |
|---|---|---|
| A1 | Empty couch | OFF, no candidates |
| A2 | Dog on the floor away from the couch | OFF |
| A3 | Dog on the floor along the cushion edge | OFF or UNKNOWN, never ON |
| A4 | Dog sniffing at the couch | OFF or UNKNOWN, never ON |
| A5 | Dog with only paws on the couch | OFF or UNKNOWN, never ON |
| B1 | Dog jumps up and sits | ON within the confirm interval |
| B2 | Dog lying down | ON sustained without flicker to OFF |
| B3 | Dog moving along the couch | One episode, no duplicates |
| B4 | Dog leaves the couch | `confirmed_clear` after an observed exit |
| C1 | Person alone on the couch | No confirmed ON |
| C2 | Person and dog together | ON, not suppressed |
| C3 | Person partly hiding the dog | ON or UNKNOWN, never OFF |
| C4 | Person fully hiding the dog | UNKNOWN, episode stays open |
| D1 | Pillows and blankets, no dog | No confirmed ON |
| D2 | Shifted cushions | Note whether polygons still fit |
| D3 | Dog partly under a blanket | ON or UNKNOWN, never a quick clear |
| E1 | Day to night transition | Note any change in confidence or false events |
| E2 | Infrared view of B1, B2, and A3 | Compare with daylight; thresholds may need to differ |
| F1 | Camera unplugged or rebooted | UNKNOWN at once, `recovered` afterwards, no clear event |
| F2 | Prolonged stale frames | UNKNOWN, reason shown |
| F3 | App restart | Starts UNKNOWN, reloads calibration |
| F4 | Invalid credentials | Sanitized message, retries stop after 3, Retry works |
| F5 | Detector failure | UNKNOWN, reason shown |
| G1 | Camera moved or rotated | Press Camera moved, confirm UNKNOWN, then redraw |

## 5. Delay measurements

Delay has separate parts that should not be merged. **Inference speed** and **decode to decision** are logged automatically in the Health tab, but they exclude upstream camera buffering, because the capture time of a frame is not available locally.

**Camera to display delay** needs a shared clock. The clock the camera burns into the picture gives a rough check against the Mac clock, but it only shows whole seconds and depends on the camera's own time being correct. Hold a phone showing a running stopwatch in view of the camera. Photograph the Mac screen and the phone together with a second device, so that a single picture shows both the real stopwatch and the stopwatch as it appears in the video. The difference is the delay. Repeat at least 10 times, and delete the photographs after reading them.

**Raw detection delay** uses the marks. Press **Dog got ON** at the moment of the visible action; the scorer then reports mark to candidate separately from the added debounce delay. Hand timing limits this to a few tenths of a second at best, so report sample count and range rather than subsecond precision. A provisional discussion target is raw detection within 2 seconds in normal conditions. Confirmed-event expectations should be agreed after this baseline exists, once the debounce values are chosen.

Comparing TCP and UDP is only worthwhile if the delay proves to be a problem. Use `./acacia probe --transport udp`, and measure loss, delay, and recovery together, since neither transport is faster in general.

## 6. Before arming the voice cue

Run disarmed first. Each confirmed ON is logged as `cue_would_play` or `cue_would_skip`, so a few days of marks show how often the clip would have played correctly, with no sound involved. Arm it only while someone is home to watch Acacia's reaction, start at a low Echo volume, and disarm at any sign of distress. Whether a cue that arrives a few seconds late helps at all is a question for a trainer.

## 7. Nonbinding discussion gates

The suggested gates are at least 95 percent event precision and 90 percent event recall on a varied agreed trial set, and no more than one false confirmed ON per day over an optional seven-day observation. Longer metadata observation still needs manual ground truth to evaluate recall. Show sample composition and limitations beside any summary metric, and rerun earlier scenarios after any tuning change.
