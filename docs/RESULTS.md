# Results: measured and pending

Last updated 2026-11-12. This document separates three kinds of evidence, because they support very different claims. **MEASURED (camera)** means observed on the real camera. **MEASURED (bench)** means observed on this Mac without the camera, using deterministic tests, a local RTSP server, or the simulator. **PENDING** means not yet done. Nothing in the MEASURED (bench) rows is evidence of accuracy on the real couch.

Headline status: the software is built, it runs on the real camera, and its failure handling is tested. On a supplied clip the detector boxes were on the dog in all 5 stills checked, and replay confirmed one of two couch visits with no false event (section 7). The app now classifies live with polygons that Zimu edited on the live view, and **no accuracy figure exists**.

## 1. Status by requirement

| Requirement | Status | Evidence |
|---|---|---|
| Architecture selected and recorded | Done | `docs/ARCHITECTURE.md` |
| Stream properties probed | MEASURED (camera) | Section 6 |
| Loopback binding, audio exclusion, secret handling, retention | MEASURED (bench and camera) | Sections 3 and 6 |
| Freshness, UNKNOWN, debounce, reconnect, stop | MEASURED (bench) | Sections 2 and 4 |
| Calibration against the live view | Done by Zimu | Three draft polygons from one clip still (revision 1), edited and saved by Zimu on the live view at 02:04 (revision 2) |
| Day, night, floor-edge, people, occlusion | PENDING | Needs Zimu and Acacia |
| Accuracy counts | PENDING | No trials run |
| Camera to display delay, raw detection delay | PENDING | Needs manual trials |
| Real-camera resource use | MEASURED (camera), 40 minutes | Section 6; a longer normal-use soak is still pending |
| Intel migration requirements | Install and 294 tests pass on GitHub's Intel runner; untested on the MacBook Pro | `docs/MIGRATION.md` |
| Passive phase 1 | MEASURED (bench) | No actuation code exists; a test pins the route list |
| Optional voice cue on Alexa | Stage 1 MEASURED (real Alexa+); recorded voice needs original Alexa | Section 8 |

## 2. Automated tests

All 301 tests pass on this Mac (MEASURED, bench): the 293 deterministic tests on 12 November and the 8 integration tests in their last run. The deterministic tests use synthetic geometry and simulated time, with no camera, model, or stored images. They cover the transition table and random-input invariants, geometry validation, classification of synthetic couch scenes, ingest buffering and backoff, redaction, credential handling, Host and Origin enforcement, retention, and scoring. The 8 integration tests run real libav against a local RTSP server that requires Digest authentication. They cover reserved characters in credentials, audio exclusion, restart of the publisher, a killed server, a frozen server, a slow consumer, and damaged packets.

Testing a clean install exposed three defects that a warm checkout had hidden, all now fixed: `setup.sh` stopped early on a fresh `config.toml`, MediaMTX wrote a self-signed key into the project folder, and a lazy import of PyAV inside the ingest error path caused a timing failure on the first run in a new environment. The clean install now passes, including the real model download and checksum verification.

## 3. Privacy checks on the running process

These were checked with `lsof`, `ps`, and `curl` against the application running on the simulated stream, and with the integration suite. The real camera will add one LAN connection in place of the loopback stream.

| Check | Result |
|---|---|
| Listening sockets | One, on `127.0.0.1` |
| Other connections | Only the RTSP stream and the browser's loopback WebSocket; no internet peers |
| Child processes | None, so no credential reaches a process argument list |
| Audio | 0 tracks opened, while the test server advertised an audio track |
| Spoofed Host or Origin | HTTP 403; a cross-origin WebSocket is refused |
| Files written | Text metadata only, owner-only, in `data/` |
| Credential leak | Confirmed that raw PyAV errors contain the URL, and that sanitized messages, logs, and health output do not |

## 4. Simulated soak

This run lasted 12 minutes on the simulated stream (1280 by 720 H.264 at 15 frames per second, `yolox_m` with CoreML), with a dog cutout moving between hidden, floor, and seat on a 60-second loop. It exercises the software and says nothing about real-world accuracy, because a pasted photograph on a drawn couch is an easy case.

The simulated dog occupied the seat in 13 separate episodes, and 13 `confirmed_on` events were emitted, with no misses, no duplicates, and no candidates cancelled. `./acacia score` printed a precision of 92.3 percent with one false event, and that figure is an artefact of the marks rather than a detection error: the run began 24 seconds into the loop with the dog already on the seat, so the first episode had no `on_start` mark. The stray `on_end` note in the same report confirms this. Added debounce was a median of 3.1 seconds (range 3.0 to 3.3, n=12) against the configured 3.0 seconds. Raw detection delay is not reported here, because the marks were scheduled from a clock with 1-second resolution.

| Injected fault | Observed behaviour |
|---|---|
| Server frozen for 8 seconds | UNKNOWN after 3 seconds (`stale_stream`), then `stream_lost`, reconnect, `recovered`, `on_resumed`. No clear event and no duplicate confirmed ON |
| Server frozen for 2 seconds | No state change, as intended for a stall below `stale_timeout_s` |
| Calibration invalidated, then restored 5 seconds later | UNKNOWN within 0.1 seconds, then `recovered`, `on_resumed` |

UNKNOWN accounted for 14 seconds, or 1.9 percent of the run. The log held no errors or tracebacks.

## 5. Performance on this Mac

All figures here are MEASURED (bench) on the MacBook Air, on mains power, and real-camera figures will differ. The first table times preprocessing, inference, and postprocessing on 1280 by 720 noise frames (30 runs each, `./acacia bench`).

| Model | CPU median | CoreML median |
|---|---|---|
| `yolox_tiny` | 45.6 ms | 8.3 ms |
| `yolox_s` | 150.9 ms | 21.4 ms |
| `yolox_m` | 359.6 ms | 33.0 ms |

The second table shows the full application on the simulated stream, at the default cadence of one inference per 0.25 seconds. The CoreML column comes from the 12-minute soak, and the CPU column from a run of about 90 seconds that also completed the state cycle correctly, which exercises the CPU fallback end to end. The CPU path cannot hold the default cadence with `yolox_m`.

| Measure | CoreML | CPU |
|---|---|---|
| Decoded frames per second | 15.0 | 15.0 |
| Inferences per second | 3.9 | 2.8 |
| Inference median (p95) | 37.9 ms (42.3) | 358.2 ms (381.7) |
| Frame age at inference, median | 35.2 ms | 32.9 ms |
| Decode to decision, median (p95) | 81.0 ms (113.8) | 398.9 ms (436.1) |
| Decision to UI acknowledgement, median (p95) | 57.1 ms (104.8) | Not sampled |
| Frame to UI acknowledgement, median (p95) | 42.9 ms (75.1) | Not sampled |
| Process CPU, percent of one core | 42 (median) | 493 (one sample) |
| Resident memory | 773 MB at start, falling to 266 MB | 409 MB (one sample) |

Memory did not grow during the soak, although macOS memory compression may contribute to the decline, so 12 minutes is not proof against a slow leak. These timings all begin at local decode. They exclude camera encoding and network buffering, and they are not physical action to display delay. For thermal behaviour, `pmset -g therm` recorded no thermal or performance warnings after the runs, and this Mac reported no CPU power status. No temperature sensor was read, and no temperature is claimed.

## 6. Real camera measurements

All of this is MEASURED (camera) on 2026-11-07 between 01:09 and 01:18 local time, over TCP, with room lights on. The application saw a colour view (mean brightness about 121 and colourfulness about 7.7), so **infrared has not been measured**, and frame rates may differ in low light. Only numbers were collected: no image from the live stream was viewed or stored during this work.

| Property | `/stream2` | `/stream1` |
|---|---|---|
| Resolution | 1280 by 720 | 2304 by 1296 |
| Codec | H.264, yuv420p | H.264, yuv420p |
| Declared frame rate | 15 | 15 |
| Measured frame rate (4 runs each, except 3 for `/stream1`) | 11.1 to 11.4 | 15.1 |
| Approximate bitrate | 260 kbit/s | 1200 kbit/s |
| Keyframe interval | 2.65 s | 1.95 s |
| Frame gap, median | 73 to 76 ms | 52 ms |
| Frame gap, p95 | 208 to 216 ms | 191 ms |
| Frame gap, maximum | 253 to 295 ms | 269 ms |
| Time to first frame | 3.0 s | 2.5 s |
| Advertised audio | PCMA | PCMA |
| Audio tracks opened | 0 | 0 |

Both streams arrive in bursts, as the gap between median and p95 shows. The declared rate of 15 is not what `/stream2` delivers, which is why the handoff was right to ask for measurement. Unauthenticated RTSP showed the methods OPTIONS, DESCRIBE, SETUP, TEARDOWN, PLAY, PAUSE, GET_PARAMETER, and SET_PARAMETER, with Basic and Digest both offered.

The next table shows the full application for 90 seconds on each stream, on mains power, with a headless client that acknowledged frames and discarded them. There was no calibration, so decision timing could not be measured.

| Measure | `/stream2` | `/stream1` |
|---|---|---|
| Decoded frames per second | 11.4 | 15.1 |
| Inferences per second | 4.0 | 3.9 |
| Inference median (p95) | 41.2 ms (47.8) | 33.0 ms (38.6) |
| Frame age at inference, median (p95, maximum) | 49 ms (134, 359) | 39 ms (117, 200) |
| Frame to UI acknowledgement, median (p95) | 51 ms (100) | 56 ms (106) |
| Process CPU, median, percent of one core | 36 | 57 |
| Resident memory at the end | 483 MB | 584 MB |
| Reconnects, decode errors, inference failures | 0 | 0 |

These runs were at different moments, so their detection counts cannot be compared. The model reported dog boxes with peak confidence 0.90 in the first run and 0.91 in the second, and person boxes throughout. Nobody looked at those live detections, so they remain unverified; section 7 covers the boxes that were checked on a clip. A credential scan of the logs and databases from these runs found no match.

After that, the application ran on `/stream1` with a real browser attached for about 40 minutes (01:21 to 02:01), with no reconnects, decode errors, or inference failures, and resident memory of 229 MB at the end. Once the draft polygons were loaded at 02:00, it reached OFF by itself in about 6 seconds. Over the first decisions (n=154), decode to decision had a median of 83 ms (p95 157) and decision to UI acknowledgement a median of 66 ms (p95 113). Like every internal timing here, these exclude camera encoding and network buffering, so they are not camera to display delay.

To compare the streams fairly, both ran at once for 120 seconds (463 paired samples). A person was detected in every sample on both streams, with median confidence 0.79 on `/stream2` and 0.83 on `/stream1`, and with near-identical median box heights (0.215 and 0.221 of the frame). The dog appeared in only 1 sample, on `/stream1` at 0.73, so **the dog comparison is inconclusive**. This is one scene over two minutes, so the difference in person confidence is suggestive and no more. Whether the two streams share a field of view was not established. `/stream1` was selected on this evidence; see `docs/ARCHITECTURE.md`.

| Still pending on the real camera | Method |
|---|---|
| Camera to display delay | `docs/VALIDATION.md`, section 5 |
| Infrared behaviour and the `IR_CHROMA_MAX` threshold | Observe a day to night transition |
| Longer normal-use soak, including overnight | Leave the application running; samples are stored each minute |

## 7. Example clip

Zimu supplied one clip from this camera, labelled as the dog on the couch: 181 seconds at 2304 by 1296 and 15 frames per second, in a colour view, with two people in view for most of it. `./acacia replay` ran it at 4 samples per second with `yolox_m`. With Zimu's permission, 8 stills were then viewed to check the results and were deleted afterwards. These stills are the only real images that have entered this work; the clip was neither copied nor changed, and the live camera view was never viewed.

**The detector.** All 5 stills containing the dog had a box on the dog, including one at confidence 0.36 with the dog partly hidden behind a person. The 3 stills from the stretch with no dog box (95, 110, and 125 seconds) showed no dog, so that stretch was true absence at those moments rather than a miss, although at 95 seconds a person close to the camera hid part of the room. Eight stills cannot establish a miss rate for the other frames.

**Classification.** This first used draft polygons estimated from one still (revision 1). Zimu then edited them on the live view (revision 2), which increased their total area by 44 percent. Replay with revision 2 produced exactly the same events as the draft, so the outcome did not depend on those edits. `ON` and `AMBIGUOUS` samples were unchanged at 88 and 28, and `OBSTRUCTED` samples fell from 107 to 43, because a person now covers a smaller share of the cushion area. The table shows revision 2. It rests on one clip in one lighting condition, so it is indicative and not a validation.

| Time in clip | What was seen | Evidence | State machine |
|---|---|---|---|
| 0 to 5 s | Dog on the floor (still at 3 s) | `CLEAR_SEEN` | UNKNOWN (start of replay) |
| About 6 to 16 s | **Dog on the couch**, mostly behind a standing person (stills at 6.5 and 14.5 s) | Brief `ON`, long `OBSTRUCTED`, brief `AMBIGUOUS` | Two raw candidates, both cancelled, **never confirmed** |
| 20 to 63 s | No dog box; not viewed | `CLEAR_EMPTY` | UNKNOWN |
| About 63 to 90 s | **Dog on the couch** (still at 75 s) | `AMBIGUOUS`, then `ON` | `confirmed_on` at 67.7 s |
| 90 to 140 s | No dog visible in 3 stills | Mostly `CLEAR_EMPTY` | UNKNOWN from 98.9 s |
| 140 to 149 s | Dog on the floor beside the couch (still at 145 s) | `AMBIGUOUS`, then `CLEAR_SEEN` | `confirmed_clear` at 149.1 s |

In event terms the clip holds two ON episodes, and the system emitted one confirmed ON event, which was correct, with no false confirmed ON. It missed the first episode. Reading these counts as 100 percent precision and 50 percent recall would be misleading at this sample size. The episode boundaries above are estimates from sampled evidence and a few stills.

Three findings follow, and each is a hypothesis to test on more clips, since one clip is not enough to tune against.

First, **a person can hide a short visit from confirmation.** The dog was on the couch for about 10 seconds, but ON evidence came in pieces of under 2 seconds, because a standing person blocked the view between them. The state was never OFF during this visit, so nothing false was asserted, but the visit went unconfirmed. Lowering `on_confirm_s` alone would trade this for more false events. A more targeted experiment is to preserve a candidate through `OBSTRUCTED` frames, up to `hold_s`, since hiding behind a person is not evidence of leaving.

Second, **a person resting on the couch keeps the state in UNKNOWN for long periods.** OFF was reached only at 149 seconds, even though the dog was off the couch for much of the clip. While the dog is unaccounted for, any person overlap blocks clearing (`lost_min_visible_fraction`), so only a floor sighting can clear it. This is conservative by design and it produced no false clear, but the state says little when someone is lying on the couch. Larger polygons did not change this, because any overlap counts. A targeted experiment is to apply the rule only where the dog was last seen, so that a person at the far end of the sectional does not block clearing.

Third, **the bottom-of-box rule did what it was meant to.** Every stretch of `ON` evidence lay inside a real couch visit, and the dog beside the couch at 140 to 149 seconds was never `ON`, although its box touched the cushions. My first reading of these results, made from box positions alone, called the first visit floor activity. That reading was wrong, which shows why box geometry should not be interpreted without ground truth.

The `cat` label also appeared in 14 samples, 11 of them during the second visit, so the model sometimes reads this dog as a cat as well. That label is shown for context and never counted.

## 8. Optional voice cue

The Mac module and the Lambda are MEASURED (bench): 66 tests with a fake network cover gating, arming, signature checks, replay refusal, retry limits, and the way the trigger secret reaches Parameter Store.

The AWS side is MEASURED (real AWS, 7 November 2026). The deployed code matched `alexa/lambda_function.py`, and the clip bucket blocks all public access. The function URL refused a request with no signature, with a wrong signature, with a valid signature 10 minutes old, and with a body altered after signing, answering 401 in each case. A signed ping returned 200 with `linked: false`, which is correct before account linking. The first request after deployment took 6.3 s, because the function had to start and fetch its secret, whereas warm requests took 0.15 to 0.26 s. A cue sent after the function has gone idle may therefore carry several seconds of extra delay. The deployment also showed that, since October 2025, a public function URL needs `lambda:InvokeFunction` as well as `lambda:InvokeFunctionUrl`. The template grants the second permission only for calls through the URL, so the function cannot be invoked directly through the Lambda API.

Stage 1 is MEASURED (real Alexa+, 7 November 2026): the private Acacia Couch sensor fired an Alexa+ routine that played a built-in sound on the chosen Echo Studio. The first attempt played nothing because no routine used the sensor yet, although Amazon accepted every report with 202; the device page in the Alexa app showed the state changes, which separated a delivery fault from a routine fault. Account linking first failed because the Login with Amazon pair had been stored where the Alexa Skill Messaging pair belongs; `./acacia cue alexa-keys` now checks a pair with Amazon before storing it. Stage 2 is BLOCKED (real Alexa+, 11 November 2026). The private Acacia Cue custom skill builds, its endpoint answers a launch request with the voice clip, and the clip's 60 s link downloads, but Alexa+ refuses to open it: both a spoken "Alexa, open acacia cue" on the Echo Studio and the same text typed into the Alexa+ app returned "Sorry, that is not supported on this device", and no request reached the function. The developer console's simulator refused en-US with "Unsupported locale". The skill's page in the Alexa app showed it enabled, with the note "Language support is limited for Alexa+ customers" and English (US) listed only for original Alexa customers. Amazon's February 2025 note to skill developers explains this: existing skills remain available on the original Alexa experience, and a skill is opened by name on Alexa+ only after Amazon evaluates it for direct invocation. A private custom skill is therefore not a path to the voice clip on an Alexa+ account. Raising the function's memory from 128 MB to 512 MB cut a cold start from 5.8 to 6.9 s down to 1.4 s, inside Alexa's 8 s limit. On 12 November the routine's action was changed to Alexa Says "Acacia, get off." on the Echo Studio, after a volume step that the routine already contained. A test trigger reached Amazon 0.97 s after the Mac sent it, and Amazon accepted the report with 202. **PENDING:** the delay from a confirmed ON to speech on the Echo, behaviour while the living room group plays music, and how Acacia responds.

While disarmed, every confirmed ON writes `cue_would_play` or `cue_would_skip` to the Events tab. Comparing those with ground-truth marks shows how often the clip would have played correctly before anything is armed. On the example clip, the confirmed visit would have been skipped, because people were in view and it was recorded during quiet hours.

## 9. Open decisions and known limitations

Zimu decided on 2026-11-07 that paws only on the couch is not ON, so that case no longer blocks acceptance scoring. Latency expectations and the debounce values should follow the baseline measurements. The default timers and thresholds are untested hypotheses.

The main limitations follow from using boxes rather than masks. A box cannot show physical support, so a floor dog close against the couch may read as AMBIGUOUS, and a dog fully hidden under a blanket looks like an empty couch; the state machine limits the second case by holding UNKNOWN and labelling the eventual `absence_timeout`, but after `lost_clear_s` it can still clear wrongly. Night performance is entirely unknown, since COCO is mainly daylight colour imagery and this camera's infrared view differs from it. Camera movement is not detected automatically, so a shifted camera with stale polygons gives wrong results until someone presses Camera moved. A frozen image that keeps arriving with fresh timestamps is not detected either, although the camera burns a running clock into the top left of the picture, which a later check could watch. Individual identity is not established, and a second dog would be handled by the aggregation rule in `docs/STATE_MACHINE.md`.
