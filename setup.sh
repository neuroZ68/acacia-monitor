#!/bin/sh
# One-time setup. Safe to re-run: it preserves config.toml, data/, and models/.
set -eu
cd "$(dirname "$0")"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for candidate in python3.13 python3.12; do
        if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
    done
fi
if [ -z "$PYTHON" ]; then
    echo "Python 3.12 or 3.13 is required. See the README." >&2
    exit 1
fi

echo "Using $("$PYTHON" --version) on $(uname -m)"
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
.venv/bin/python -m pip install --quiet --no-deps -e .

if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    chmod 600 config.toml
    echo "Created config.toml. Set camera.host before starting."
fi

./acacia model fetch
echo "Setup finished. Next: ./acacia credentials set, then ./acacia run"
