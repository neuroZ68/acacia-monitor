"""Local camera credentials: macOS Keychain, or an owner-only file.

Nothing here reads environment variables or command-line arguments, because
both leak (process listings, debug output, child processes, shell history).
"""

from __future__ import annotations

import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from acacia.sanitize import register_secret

USERNAME_ACCOUNT = "rtsp-username"
PASSWORD_ACCOUNT = "rtsp-password"
CUE_SECRET_ACCOUNT = "cue-trigger-secret"


class CredentialError(Exception):
    """Credentials are missing or unsafe. Messages never include secret values."""


@dataclass(frozen=True, repr=False)
class CameraCredentials:
    username: str
    password: str

    def __repr__(self) -> str:
        return "CameraCredentials(<redacted>)"


class RtspUrl:
    """An authenticated URL whose text form never shows the credentials."""

    __slots__ = ("_value", "redacted")

    def __init__(self, host: str, port: int, path: str, creds: CameraCredentials | None):
        tail = f"{host}:{port}/{path.lstrip('/')}"
        self.redacted = f"rtsp://{tail}"
        if creds is None:
            self._value = self.redacted
        else:
            # safe="" percent-encodes reserved characters such as @ : / ? #.
            user = quote(creds.username, safe="")
            secret = quote(creds.password, safe="")
            self._value = f"rtsp://{user}:{secret}@{tail}"

    def reveal(self) -> str:
        """Only for the libav open call. Never log the return value."""
        return self._value

    def __repr__(self) -> str:
        return f"RtspUrl({self.redacted})"

    __str__ = __repr__


def _keychain():
    # Pinned to the real backend so keyring cannot silently fall back to a
    # plaintext or null store.
    from keyring.backends.macOS import Keyring

    return Keyring()


def store_keychain(service: str, username: str, password: str) -> None:
    keychain = _keychain()
    keychain.set_password(service, USERNAME_ACCOUNT, username)
    keychain.set_password(service, PASSWORD_ACCOUNT, password)


def store_cue_secret(service: str, value: str) -> None:
    _keychain().set_password(service, CUE_SECRET_ACCOUNT, value)


def load_cue_secret(service: str) -> str:
    value = _keychain().get_password(service, CUE_SECRET_ACCOUNT)
    if not value:
        raise CredentialError("No cue secret in the Keychain. Run: ./acacia cue new-secret")
    register_secret(value)
    return value


def delete_keychain(service: str) -> int:
    from keyring.errors import PasswordDeleteError

    keychain = _keychain()
    removed = 0
    for account in (USERNAME_ACCOUNT, PASSWORD_ACCOUNT, CUE_SECRET_ACCOUNT):
        try:
            keychain.delete_password(service, account)
            removed += 1
        except PasswordDeleteError:
            pass
    return removed


def _load_keychain(service: str) -> CameraCredentials:
    keychain = _keychain()
    username = keychain.get_password(service, USERNAME_ACCOUNT)
    password = keychain.get_password(service, PASSWORD_ACCOUNT)
    if not username or not password:
        raise CredentialError(
            "No camera credentials in the Keychain. Run: ./acacia credentials set"
        )
    return CameraCredentials(username, password)


def _load_file(path: Path) -> CameraCredentials:
    if not path.exists():
        raise CredentialError(f"Secret file not found: {path.name}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise CredentialError(
            f"{path.name} is readable by other users (mode {mode:o}). Run: chmod 600 {path.name}"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        # The decoder error can quote file content, so it is deliberately dropped.
        raise CredentialError(f"{path.name} is not valid TOML") from None
    username, password = data.get("username"), data.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
        raise CredentialError(f"{path.name} must define username and password")
    return CameraCredentials(username, password)


def load_credentials(backend: str, service: str, file_path: Path) -> CameraCredentials:
    if backend == "keychain":
        creds = _load_keychain(service)
    elif backend == "file":
        creds = _load_file(file_path)
    else:
        raise CredentialError(f"Unknown credential backend: {backend}")
    register_secret(creds.username)
    register_secret(creds.password)
    return creds
