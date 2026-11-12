#!/usr/bin/env python3
"""Stand-in RTSP camera for exercising the full app without real hardware.

Streams a drawn scene with a dog cutout that moves between hidden, floor, and
seat on a fixed 60-second loop, through a local MediaMTX server. Useful for
checking the pipeline and UI end to end. A pasted photo on a drawn couch proves
nothing about accuracy on a real couch.

Needs ffmpeg and mediamtx on PATH, plus any photo of a dog:
    .venv/bin/python tools/sim_camera.py --dog photo.jpg --crop 130,225,307,547 --work /tmp/sim
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 1280, 720
PORT = 18554
USER, PASSWORD = "simcam", "local-sim-only"
DOG_SIZE = (140, 240)
FLOOR_Y = 450
SEAT_Y = 180
SEAT_POLYGON = [[0.2, 0.40], [0.9, 0.40], [0.9, 0.62], [0.2, 0.62]]


def draw_room(path: Path) -> None:
    room = Image.new("RGB", (WIDTH, HEIGHT), (176, 170, 158))
    draw = ImageDraw.Draw(room)
    draw.rectangle([0, 360, WIDTH, HEIGHT], fill=(120, 104, 88))  # floor
    draw.rectangle([256, 120, 1152, 300], fill=(92, 78, 70))      # backrest
    draw.rectangle([256, 288, 1152, 446], fill=(122, 104, 92))    # seat
    draw.rectangle([256, 446, 1152, 500], fill=(70, 58, 52))      # front face
    draw.line([(704, 288), (704, 446)], fill=(92, 78, 70), width=4)
    draw.text((20, 16), "SIMULATED", fill=(255, 255, 255), font_size=30)
    room.save(path)


def write_config(path: Path) -> None:
    path.write_text(textwrap.dedent(f"""\
        logLevel: error
        api: no
        metrics: no
        pprof: no
        playback: no
        rtsp: yes
        rtspTransports: [tcp]
        rtspAddress: 127.0.0.1:{PORT}
        rtspAuthMethods: [digest]
        rtmp: no
        hls: no
        webrtc: no
        srt: no
        authMethod: internal
        authInternalUsers:
        - user: any
          permissions:
          - action: publish
        - user: {USER}
          pass: {PASSWORD}
          permissions:
          - action: read
        paths:
          all_others:
        """))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dog", type=Path, required=True)
    parser.add_argument("--crop", help="left,top,right,bottom pixels of the dog in the photo")
    parser.add_argument("--work", type=Path, required=True, help="scratch directory")
    args = parser.parse_args()

    args.work.mkdir(parents=True, exist_ok=True)
    room_path, dog_path, config_path = args.work / "room.png", args.work / "dog.png", args.work / "mediamtx.yml"
    draw_room(room_path)
    dog = Image.open(args.dog).convert("RGB")
    if args.crop:
        dog = dog.crop(tuple(int(v) for v in args.crop.split(",")))
    dog.resize(DOG_SIZE).save(dog_path)
    write_config(config_path)

    # 0-10 s hidden, 10-20 floor, 20-40 seat, 40-50 floor, 50-60 hidden.
    t = "mod(t,60)"
    x = f"if(lt({t},10)+gte({t},50),-400,560)"
    y = f"if(between({t},20,40),{SEAT_Y},{FLOOR_Y})"
    # MediaMTX writes a self-signed key into its working directory, so keep it in scratch.
    server = subprocess.Popen(["mediamtx", str(config_path)], stdout=subprocess.DEVNULL, cwd=args.work)
    time.sleep(1)
    publisher = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-loop", "1", "-framerate", "15", "-i", str(room_path),
         "-loop", "1", "-framerate", "15", "-i", str(dog_path),
         "-filter_complex", f"[0][1]overlay=x='{x}':y='{y}',format=yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "30",
         "-f", "rtsp", "-rtsp_transport", "tcp", f"rtsp://127.0.0.1:{PORT}/stream2"],
        stdin=subprocess.DEVNULL)

    def shutdown(*_):
        publisher.terminate()
        server.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Simulated camera at rtsp://127.0.0.1:{PORT}/stream2", flush=True)
    publisher.wait()
    server.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
