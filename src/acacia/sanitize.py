"""Redaction for anything that might carry camera credentials.

libav and PyAV both echo the full input URL in their error text, so every error
string from the ingest path has to pass through here before it reaches a log,
the health endpoint, or the terminal.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import traceback
from urllib.parse import quote

_URL_USERINFO = re.compile(r"(?i)\b(rtsps?|https?)://[^\s/'\"@]*@")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization:\s*)\S.*")

_lock = threading.Lock()
_known_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Remember a secret so it is scrubbed even outside URL syntax."""
    if not value:
        return
    with _lock:
        _known_secrets.add(value)
        _known_secrets.add(quote(value, safe=""))


def clear_registered_secrets() -> None:
    with _lock:
        _known_secrets.clear()


def scrub(text: object) -> str:
    """Return text with URL userinfo, auth headers, and known secrets removed."""
    out = str(text)
    out = _URL_USERINFO.sub(lambda m: f"{m.group(1)}://***@", out)
    out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}***", out)
    with _lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for secret in secrets:
        out = out.replace(secret, "***")
    return out


class ScrubFilter(logging.Filter):
    """Flatten and scrub every log record, including exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = scrub("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


def install_exception_hooks() -> None:
    """Scrub uncaught exceptions on the main thread and on worker threads."""

    def main_hook(exc_type, exc, tb):
        print(scrub("".join(traceback.format_exception(exc_type, exc, tb))), file=sys.stderr)

    def thread_hook(args):
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        print(scrub(text), file=sys.stderr)

    sys.excepthook = main_hook
    threading.excepthook = thread_hook
