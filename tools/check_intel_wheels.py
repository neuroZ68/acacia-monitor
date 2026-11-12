#!/usr/bin/env python3
"""Check that every runtime pin has an Intel macOS wheel on PyPI.

Run before migrating, or after changing requirements.txt:
    python3 tools/check_intel_wheels.py [--python cp313]
This confirms availability only. It proves nothing about running on Intel.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path


def wheel_fits(name: str, python_tag: str) -> bool:
    match = re.search(r"-(cp\d+|py\d(?:\.py\d)?)-(abi3|none|cp\d+t?)-(.+)\.whl$", name)
    if not match:
        return False
    tag, abi, platform = match.groups()
    if abi.endswith("t"):  # free-threaded builds do not install on a standard interpreter
        return False
    if "x86_64" not in platform and "universal2" not in platform and platform != "any":
        return False
    if platform != "any" and "macosx" not in platform:
        return False
    if tag.startswith("py"):
        return True
    wanted = int(python_tag[2:])
    version = int(tag[2:])
    return version == wanted or (abi == "abi3" and version <= wanted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="cp313")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    failures = 0
    for line in (root / "requirements.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==")
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as reply:
            files = json.load(reply)["urls"]
        fits = sorted(f["filename"] for f in files if wheel_fits(f["filename"], args.python))
        print(f"{'ok  ' if fits else 'MISS'} {name}=={version}  {fits[0] if fits else 'no Intel wheel'}")
        failures += not fits
    print(f"{failures} package(s) without an Intel wheel for {args.python}.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
