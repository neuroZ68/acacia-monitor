# Working on Acacia Camera

Read `docs/RESULTS.md` first. It records what has been measured on the real camera, what was measured on the bench, and what is still pending, and every change to behaviour or configuration should keep it current.

## Rules that do not bend

- **Secrets never pass through a conversation.** Camera credentials go in only at `./acacia credentials set`, the cue secret only through `./acacia cue new-secret`, and the Alexa key pair only through `./acacia cue alexa-keys`, all hidden local prompts. Never ask for a credential in chat, never type or paste one into a field, and never put one in a file, an argument, or a log.
- **No real camera frames in a conversation.** Do not screenshot or read pixels from the live camera; use the API's metadata instead, and leave drawing the cushion polygons to the owner. Screenshots of the simulator (`tools/sim_camera.py`) are fine. Looking at frames from a supplied clip needs the owner's permission for that clip, and extracted stills are deleted afterwards.
- **Keep the privacy defaults.** The UI binds to `127.0.0.1`, audio is never set up, and images and video are never written to disk.
- **This repository is public.** `config.toml`, `data/`, `models/`, the function URL, skill IDs, and AWS resource names stay out of it. Run `./acacia check-secrets` before every commit.
- **Report evidence as it is.** Never imply an accuracy figure or a real-camera result that has not been measured, and label bench results as bench results.

## Household devices

The voice cue makes a real Echo speak in a home. Confirm before triggering it late at night, and before any change that affects the whole household, such as moving the Alexa account between Alexa+ and the original Alexa.

## Commands

```bash
./setup.sh
```

```bash
.venv/bin/python -m pytest -m "not integration"
```

```bash
./acacia check-secrets
```
