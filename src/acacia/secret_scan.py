"""Pre-commit secret check. Reports file and line only, never the matched text."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PATTERNS = {
    "URL with embedded credentials": re.compile(r"(?i)\b(rtsps?|https?)://[^\s/'\"{}<>]+:[^\s/'\"{}<>@]+@"),
    "password assignment": re.compile(r"""(?i)\bpassword\s*=\s*['"][^'"\s{]{3,}['"]"""),
}
# Fake values for redaction tests and the throwaway local RTSP test server.
ALLOWED_FILES = {
    "tests/test_alexa_lambda.py",
    "tests/test_credentials.py",
    "tests/test_cue.py",
    "tests/test_integration_rtsp.py",
    "tests/test_sanitize.py",
    "tests/test_secret_scan.py",
    "tools/sim_camera.py",
}


def repository_files(root: Path) -> list[str]:
    """Tracked plus untracked files that .gitignore does not exclude."""
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root,
                         capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def scan(root: Path, conf=None) -> list[str]:
    literals: list[str] = []
    if conf is not None:
        from acacia.credentials import CredentialError, load_credentials

        try:
            creds = load_credentials(conf.backend, conf.service, root / conf.file)
            literals = [creds.password, creds.username]
        except CredentialError:
            pass

    findings: list[str] = []
    for name in repository_files(root):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if name not in ALLOWED_FILES:
                for label, pattern in PATTERNS.items():
                    if pattern.search(line):
                        findings.append(f"{name}:{number}: {label}")
            for index, literal in enumerate(literals):
                if len(literal) >= 4 and literal in line:
                    kind = "camera password" if index == 0 else "camera username"
                    findings.append(f"{name}:{number}: contains the stored {kind}")
    return findings
