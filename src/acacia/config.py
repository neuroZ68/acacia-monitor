"""Nonsecret configuration: typed, validated, and strict about unknown keys."""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class CameraConfig:
    name: str = "living_room_cam"
    host: str = ""
    port: int = 554
    path: str = "/stream1"
    transport: str = "tcp"
    rotation_deg: int = 0
    open_timeout_s: float = 8.0
    read_timeout_s: float = 3.0
    reconnect_max_backoff_s: float = 30.0
    auth_max_attempts: int = 3


@dataclass(frozen=True)
class CredentialsConfig:
    backend: str = "keychain"
    service: str = "acacia-camera"
    file: str = "secrets.local.toml"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    ui_fps: float = 10.0
    jpeg_quality: int = 72


@dataclass(frozen=True)
class DetectorConfig:
    model: str = "yolox_m"
    provider: str = "auto"
    inference_interval_s: float = 0.25
    min_confidence: float = 0.20
    dog_confidence: float = 0.40
    person_confidence: float = 0.35
    nms_iou: float = 0.45
    context_classes: tuple[str, ...] = ("cat", "sheep", "cow", "bear", "teddy bear")


@dataclass(frozen=True)
class ClassifierConfig:
    on_support: float = 0.60
    ambiguous_support: float = 0.20
    ambiguous_box_overlap: float = 0.35
    occlusion_fraction: float = 0.30
    min_visible_fraction: float = 0.50
    lost_min_visible_fraction: float = 0.95


@dataclass(frozen=True)
class StateConfig:
    on_confirm_s: float = 3.0
    clear_confirm_s: float = 5.0
    lost_clear_s: float = 60.0
    evidence_gap_s: float = 1.5
    hold_s: float = 10.0
    stale_timeout_s: float = 3.0
    recovery_warmup_s: float = 2.0


@dataclass(frozen=True)
class RetentionConfig:
    max_events: int = 20000
    max_age_days: int = 30
    metrics_interval_s: float = 60.0
    log_max_bytes: int = 5_000_000
    log_backups: int = 3


@dataclass(frozen=True)
class CueConfig:
    enabled: bool = False
    endpoint: str = ""
    cooldown_s: float = 600.0
    max_per_day: int = 6
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    silent_when_people: bool = True
    people_clear_s: float = 30.0
    clear_after_s: float = 35.0


@dataclass(frozen=True)
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    state: StateConfig = field(default_factory=StateConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    cue: CueConfig = field(default_factory=CueConfig)


# Settings the UI may change at runtime, with their allowed ranges. Everything
# else needs a config.toml edit and a restart.
TUNABLES: dict[str, tuple[float, float]] = {
    "detector.inference_interval_s": (0.05, 5.0),
    "detector.min_confidence": (0.01, 0.95),
    "detector.dog_confidence": (0.01, 0.99),
    "detector.person_confidence": (0.01, 0.99),
    "classifier.on_support": (0.05, 1.0),
    "classifier.ambiguous_support": (0.0, 1.0),
    "classifier.ambiguous_box_overlap": (0.0, 1.0),
    "classifier.occlusion_fraction": (0.0, 1.0),
    "classifier.min_visible_fraction": (0.0, 1.0),
    "classifier.lost_min_visible_fraction": (0.0, 1.0),
    "state.on_confirm_s": (0.0, 120.0),
    "state.clear_confirm_s": (0.0, 600.0),
    "state.lost_clear_s": (0.0, 3600.0),
    "state.evidence_gap_s": (0.0, 60.0),
    "state.hold_s": (0.0, 600.0),
    "state.stale_timeout_s": (0.5, 120.0),
    "state.recovery_warmup_s": (0.0, 60.0),
    "retention.max_events": (100, 1_000_000),
    "retention.max_age_days": (1, 3650),
}


def _build_section(cls: type, raw: dict[str, Any], section: str):
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"Unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for key, value in raw.items():
        default = getattr(cls(), key)
        if isinstance(default, tuple):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{section}.{key} must be a list of strings")
            value = tuple(value)
        elif isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        elif type(value) is not type(default):
            raise ConfigError(f"{section}.{key} must be {type(default).__name__}")
        values[key] = value
    return cls(**values)


def _from_dict(raw: dict[str, Any]) -> Config:
    sections = {f.name: f.default_factory for f in dataclasses.fields(Config)}
    unknown = set(raw) - set(sections)
    if unknown:
        raise ConfigError(f"Unknown section(s): {', '.join(sorted(unknown))}")
    return Config(**{name: _build_section(cls, raw.get(name, {}), name) for name, cls in sections.items()})


def validate(cfg: Config) -> None:
    cam, srv, st, det, cl = cfg.camera, cfg.server, cfg.state, cfg.detector, cfg.classifier
    if not cam.host or cam.host == "CAMERA_IP_HERE":
        raise ConfigError("Set camera.host in config.toml")
    if "@" in cam.host or "/" in cam.host:
        raise ConfigError("camera.host must be a bare host or IP. Credentials do not belong in config")
    if not cam.path.startswith("/"):
        raise ConfigError("camera.path must start with /")
    if cam.transport not in ("tcp", "udp"):
        raise ConfigError("camera.transport must be tcp or udp")
    if cam.rotation_deg not in (0, 90, 180, 270):
        raise ConfigError("camera.rotation_deg must be 0, 90, 180, or 270")
    if cfg.credentials.backend not in ("keychain", "file"):
        raise ConfigError("credentials.backend must be keychain or file")
    try:
        loopback = ipaddress.ip_address(srv.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError(
            "server.host must be a loopback address. LAN access needs authentication, "
            "which phase 1 does not implement"
        )
    if det.provider not in ("auto", "cpu", "coreml"):
        raise ConfigError("detector.provider must be auto, cpu, or coreml")
    from acacia.detector import COCO_CLASSES

    unknown = set(det.context_classes) - set(COCO_CLASSES)
    if unknown:
        raise ConfigError(f"detector.context_classes has unknown labels: {', '.join(sorted(unknown))}")
    if det.dog_confidence < det.min_confidence:
        raise ConfigError("detector.dog_confidence must be at least detector.min_confidence")
    if cl.ambiguous_support > cl.on_support:
        raise ConfigError("classifier.ambiguous_support must not exceed classifier.on_support")
    if st.hold_s < st.clear_confirm_s:
        raise ConfigError(
            "state.hold_s must be at least state.clear_confirm_s, or an observed exit "
            "could never reach OFF directly from ON"
        )
    if st.stale_timeout_s < st.evidence_gap_s:
        raise ConfigError("state.stale_timeout_s must be at least state.evidence_gap_s")
    if st.lost_clear_s < st.clear_confirm_s:
        raise ConfigError("state.lost_clear_s must be at least state.clear_confirm_s")
    cue = cfg.cue
    if cue.enabled and not cue.endpoint.startswith("https://"):
        raise ConfigError("cue.endpoint must be an https URL when the cue is enabled")
    if "@" in cue.endpoint or "?" in cue.endpoint:
        raise ConfigError("cue.endpoint must be a plain URL. Secrets do not belong in config")
    if cue.clear_after_s < 30:
        raise ConfigError("cue.clear_after_s must be at least 30, which Amazon requires for sensors")
    for name in ("quiet_start", "quiet_end"):
        if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", getattr(cue, name)):
            raise ConfigError(f"cue.{name} must look like 22:00")


def load_config(path: Path, overrides_path: Path | None = None) -> Config:
    if not path.exists():
        raise ConfigError(f"{path.name} not found. Run ./setup.sh or copy config.example.toml")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None
    cfg = _from_dict(raw)
    if overrides_path and overrides_path.exists():
        cfg = apply_overrides(cfg, json.loads(overrides_path.read_text(encoding="utf-8")))
    validate(cfg)
    return cfg


def apply_overrides(cfg: Config, overrides: dict[str, Any]) -> Config:
    """Return cfg with dotted tunable overrides applied and range-checked."""
    for key, value in overrides.items():
        if key not in TUNABLES:
            raise ConfigError(f"{key} is not a runtime-tunable setting")
        low, high = TUNABLES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key} must be a number")
        if not low <= value <= high:
            raise ConfigError(f"{key} must be between {low} and {high}")
        section_name, name = key.split(".")
        section = getattr(cfg, section_name)
        typed = type(getattr(section, name))(value)
        cfg = dataclasses.replace(cfg, **{section_name: dataclasses.replace(section, **{name: typed})})
    return cfg


def tunable_values(cfg: Config) -> dict[str, float]:
    return {k: getattr(getattr(cfg, k.split(".")[0]), k.split(".")[1]) for k in TUNABLES}


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write via a temp file and rename, so a crash cannot leave a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
