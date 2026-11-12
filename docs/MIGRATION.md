# Migration to the Intel MacBook Pro

None of this has been tested on the destination machine. The destination's exact CPU, memory, graphics card, and macOS version are unverified, so inspect them first. Everything below is a plan based on published wheels, and accuracy and speed results from the MacBook Air do not transfer.

## 1. Requirements

The pinned dependencies need Python 3.12 or 3.13 and macOS 13 or later, because `onnxruntime` 1.23.2 publishes its Intel wheel for macOS 13. Keep `onnxruntime` at 1.23.2, since later releases have no Intel wheels. Do not assume Docker offers graphics passthrough, and note that this application does not use Docker.

## 2. Steps

The repository's continuous integration installs the pinned dependencies and runs the deterministic tests on a GitHub-hosted Intel Mac. That shows the install works on x86_64, and it says nothing about speed or accuracy on this particular machine.

1. Record the hardware with `system_profiler SPHardwareDataType SPDisplaysDataType` and `sw_vers`.
2. Stop the application on the old Mac with `./acacia stop`, so that two machines never watch and cue at the same time.
3. Install Python 3.13 (the python.org installer is universal) and git. The AWS CLI version 2 is needed only to maintain the voice cue, and `ffmpeg` and `mediamtx` only for the integration tests and `alexa/upload-clip.sh`.
4. Clone the repository, run `python3 tools/check_intel_wheels.py`, then run `./setup.sh`. The virtual environment is architecture-specific and cannot be copied.
5. Copy the machine-local files from the old Mac into the clone, keeping them owner-only: `config.toml`, and `data/calibration.json` only if the camera has not moved. Both are gitignored. Confirm `camera.host`, since DHCP may have changed the address.
6. Run `./acacia credentials set`. Keychain items do not move with the project.
7. For the voice cue, run `aws login`, then `./acacia cue new-secret`. This stores a new trigger secret in this Mac's Keychain and in Parameter Store, which also stops the old Mac from triggering. Nothing on AWS or Alexa needs to change. `./acacia cue check` then sends a signed ping and should answer `linked: True`.
8. Run `./acacia probe` and compare the result with `docs/RESULTS.md`.
9. Run `./acacia bench` to measure both providers on the real hardware.
10. Choose a detector setting from the measurement below, then run `./acacia run`.
11. Confirm in the Cushions tab that calibration is valid against the live view, and redraw if not.
12. Run the test suite with `.venv/bin/python -m pip install -r requirements-dev.txt`, then `.venv/bin/python -m pytest -m "not integration"`.

A laptop that sleeps stops watching. `caffeinate -i ./acacia run` keeps the Mac awake for as long as the application runs, without changing any system setting, and a closed lid still sends a MacBook to sleep unless it has power and an external display. Neither has been tested on this machine.

## 3. Choosing the detector setting

On GitHub's Intel runner (macOS 15.7.9, 12 November 2026), the `onnxruntime` 1.23.2 Intel wheel listed `CoreMLExecutionProvider` alongside the CPU provider, and all 294 deterministic tests passed. Whether CoreML runs faster than the CPU on this machine's AMD graphics card is unknown, so `./acacia bench` decides. On the MacBook Air, the CPU path took about 360 ms per frame with `yolox_m` and about 150 ms with `yolox_s`, and an older Intel CPU may be slower. Inference must finish well inside `inference_interval_s`. If it does not, the first steps are `model = "yolox_s"` in `config.toml` (run `./acacia model fetch yolox_s` first) or a longer interval. A smaller model can miss detections that a larger one finds, particularly in infrared, so treat that change as a new configuration to validate rather than a free speed-up.

The stream is a separate choice. `/stream1` costs more to decode than `/stream2`, and on the MacBook Air the whole application used about 57 percent of one core compared with 36 percent. If an older CPU struggles, `/stream2` is the fallback, but calibration is tied to the stream, so changing it means redrawing the polygons, and it counts as a changed configuration.

Sustained CPU inference on a laptop generates heat. Observe normal use and report what the machine shows, without adding privileged tools just to read a temperature.

## 4. Required revalidation

Rerun these on the Intel machine and record them separately: the stream probe, the detector benchmark, resource use over a stated duration, the fault scenarios (F1 to F5), and enough of the accuracy matrix to cover both lighting conditions. CoreML and CPU outputs agreed on the test photograph, but a different provider or a different model counts as a changed configuration for scoring.
