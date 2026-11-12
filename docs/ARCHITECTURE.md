# Architecture decision

This record covers milestone M0 for Zimu and Assemble to review. The selected design is a small native Python worker with a local web interface (Option A in the handoff). The project lives in `~/Code/Acacia_monitor`, the folder this work was started in, rather than the suggested `~/Projects/acacia-camera`.

## 1. Inspected hardware

The prototype machine is a MacBook Air (Mac16,13) with an Apple M4 (4 performance and 6 efficiency cores, 10 graphics cores), 32 GB of memory, and macOS 26.6.2. Python 3.13.7, ffmpeg 8.1.2, Docker 29.2.1, and Homebrew were already installed. The camera answered on port 554 of the local network and offered both Basic and Digest authentication. The destination 2019 16-inch Intel MacBook Pro was not available for inspection, so every statement about it is a plan rather than a measurement.

## 2. Native worker compared with Frigate

The deciding requirement is explicit uncertainty about support on a cushion, and this is where the two options differ most. Frigate's zones evaluate the bottom center of a bounding box, and its documentation states that "it does not matter how much of the bounding box overlaps with the zone". Zone membership with `inertia` and `loitering_time` is therefore a good fit for a yes-or-no presence signal. On the other hand, the handoff requires an UNKNOWN state, separate evidence for lower-body support, person occlusion rules, a transition table that can be tested without a model, and health-driven state changes, and none of these map onto Frigate's zone semantics. They would have to be rebuilt as an external consumer of Frigate's events, leaving a second system to operate.

Deployment also favours the native worker on this hardware. Frigate's documentation states that the "NPU in Apple Silicon can't be accessed from within a container", so acceleration here would require a separate detector client on the host connected to the container over ZMQ. On the Intel machine, Docker Desktop runs containers in a Linux virtual machine and graphics passthrough cannot be assumed, so Frigate should be expected to use OpenVINO on the CPU. Frigate also brings recording and review features that phase 1 does not want. Its strengths are worth noting for later: if the project ever needs recording, many cameras, or Home Assistant integration, Frigate would be the better base. Generally, Option A is smaller for this specific question, and it keeps every behaviour the handoff cares about in code that can be tested directly.

## 3. Selected components

| Concern | Choice | Reason |
|---|---|---|
| Ingest and decode | PyAV 19.0.2 in-process, RTSP over TCP | The authenticated URL never enters a subprocess argument list, and `allowed_media_types=video` keeps audio from being set up on the wire |
| Handoff | One-slot overwrite buffer | Memory cannot grow with consumer lag |
| Runtime | ONNX Runtime 1.23.2, CoreML or CPU provider | One runtime on both Macs, with a CPU path on each |
| Model | YOLOX-m (COCO), 640 pixel input, ONNX | Apache-2.0, and fast enough here (see `docs/RESULTS.md`) |
| Decision logic | Own geometry and state modules | Pure functions and an explicit clock, tested without a model |
| Interface | Starlette, Uvicorn, plain JavaScript and canvas | No build step and no external assets |
| Persistence | SQLite for bounded metadata, JSON for calibration and settings | Standard library, atomic writes, no image columns |
| Credentials | macOS Keychain, or a `chmod 600` file | Neither touches environment variables or arguments |

The camera offers two streams, and `/stream1` was selected after measurement. It delivers a true 15 frames per second at about 1.2 Mbit/s, compared with about 11 frames per second at about 260 kbit/s for `/stream2`, and it costs about 21 percentage points more of one core and about 100 MB more memory on this Mac. The model input is 640 pixels either way, so the expected gain comes from less compression rather than more resolution, which should matter most in infrared. Its extra pixels would also allow cropping to the couch later, should detail prove to be the limiting factor. The measured evidence for accuracy is modest: at the same moments, person confidence had a median of 0.83 compared with 0.79, and the dog was not in view long enough to compare. The choice is reversible, but it should be settled before calibration, because polygons are tied to the stream.

The application runs three threads. The ingest thread decodes every packet and overwrites the frame slot. The inference thread takes the newest frame at the configured cadence and calls the detector. The decision thread runs on its own timer, so a hung decoder or model cannot also stop the watchdog, and the state still falls to UNKNOWN. The detector boundary is a single `detect(frame, thresholds)` method, so a different model needs one new class and no change elsewhere.

## 4. Model provenance and licences

The detector is the official YOLOX ONNX export from the `0.1.1rc0` release of `Megvii-BaseDetection/YOLOX`, verified by SHA-256 on load and on download. The hashes in `src/acacia/detector.py` were computed from those files on 2026-11-07, because the release publishes none. ONNX was chosen over a PyTorch checkpoint because it is a data format that does not execute code when loaded. The decode path was checked against a public test photograph: the dog box agreed with the expected position, and CoreML and CPU outputs differed by at most 0.01. This confirms correct decoding only and says nothing about accuracy on this couch.

License was a selection criterion. Ultralytics YOLO is AGPL-3.0 and YOLOv9 is GPL-3.0, both of which constrain later distribution, whereas YOLOX is Apache-2.0. The weights were trained on COCO, whose terms page could not be retrieved during this work, so the training-data terms remain unverified and should be checked before any commercial use.

| Component | Licence | Distribution note |
|---|---|---|
| YOLOX code and weights | Apache-2.0 | Downloaded by `setup.sh`, not committed |
| ONNX Runtime | MIT | None |
| PyAV | BSD-3-Clause | Wheel bundles FFmpeg, which reports "LGPL version 3 or later", plus libx264 and libx265, which are GPL projects |
| Pillow | MIT-CMU | None |
| Starlette, Uvicorn, websockets, psutil, NumPy | BSD family | None |
| keyring | MIT | None |
| MediaMTX (development only) | MIT | Never part of the application |

Private local use is unaffected by any of these. In contrast, shipping a packaged binary containing the PyAV wheel would need a review of the FFmpeg, x264, and x265 obligations, even though this application only decodes and never calls those encoders.

## 5. Acceleration and the Intel path

CoreML acceleration is measured rather than assumed: `yolox_m` took 33 ms with the CoreML provider and 360 ms on the CPU on this Mac. The provider setting `auto` selects CoreML when the installed runtime offers it, and otherwise the CPU. The wheel also lists an Azure provider, which is never selected because providers are named explicitly.

The main migration constraint is that current wheels have dropped Intel Macs. ONNX Runtime 1.31.1 and OpenVINO 2026.4.1 publish no Intel macOS wheels, and the last versions that do are 1.23.2 and 2025.4.1 respectively. Consequently, ONNX Runtime is pinned at 1.23.2 on both machines, which keeps the runtime identical across the migration, at the cost of forgoing newer releases. `tools/check_intel_wheels.py` confirms that every pin in `requirements.txt` has an Intel wheel for Python 3.13. Pillow was chosen over OpenCV for a related reason: the current OpenCV wheels need macOS 14 on Intel, while Pillow's support older releases. On GitHub's Intel runner, the Intel wheel lists the CoreML provider, and whether that helps on an AMD graphics card is unknown. `docs/MIGRATION.md` lists the checks to run there.

## 6. Network activity

Installation downloads Python packages from PyPI and the model from GitHub. Normal operation needs no internet connection. At runtime the process holds one RTSP connection to the camera and one loopback listener, which was confirmed with `lsof` on the running process. The optional cue adds outbound HTTPS to the Lambda, only when armed. No telemetry is implemented. Loopback binding alone does not protect a camera feed, because browsers do not apply same-origin rules to WebSockets, so any web page could otherwise connect to the local socket. Every request therefore has its Host and Origin headers checked.

## 7. Optional voice cue

After phase 1, Zimu asked for a voice clip to play on a living room Echo when Acacia is confirmed on the couch. The handoff excluded this from phase 1, so it is built as a separate module that is off by default, starts disarmed at every launch, and logs what it would have done while disarmed. Bluetooth playback from the Mac would have been simpler and fully local, and Voice Monkey would have been quicker to build, but Zimu chose a private Alexa skill.

A skill cannot speak unprompted, so the design chains four steps. The Mac sends a signed request to a Lambda Function URL, the Lambda sends a ChangeReport for a virtual motion sensor, an Alexa routine reacts to that sensor by opening a custom skill on a chosen Echo, and the skill plays the clip from a private S3 bucket through a presigned link. AWS is unavoidable because Amazon documents smart home code as hosted on Lambda. Cloudflare could have hosted the custom skill, but it would have added a platform without removing one. Amazon's custom routine triggers would be cleaner, but they are a developer preview that needs Amazon's permission and a published skill. **Whether a private sensor can fire a routine that opens a private skill under Alexa+ is unverified**, so `alexa/README.md` tests that before anything else.

## 8. Fallbacks

If CoreML misbehaves, `provider = "cpu"` uses the same model. If the CPU cannot sustain the cadence, `model = "yolox_s"` or a longer `inference_interval_s` trades detail for speed, and that change should be measured rather than assumed. If geometry fails on the real couch, the handoff's order applies: first review placement, polygons, and uncertainty rules, and then try a modest alternate local detector against the identified failure mode. An instance segmentation model would be the natural next experiment, since a mask speaks more directly to support than a box does, but it is not justified until a failure mode calls for it.
