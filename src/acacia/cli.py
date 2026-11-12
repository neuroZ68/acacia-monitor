"""Command-line entry point. Heavy imports stay inside commands."""

from __future__ import annotations

import argparse
import getpass
import os
import signal
import sys
import time
from pathlib import Path

from acacia.sanitize import install_exception_hooks, scrub

ROOT = Path(os.environ.get("ACACIA_ROOT", Path(__file__).resolve().parents[2]))
CONFIG_PATH = ROOT / "config.toml"
DATA_DIR = ROOT / "data"
MODELS_DIR = ROOT / "models"
PID_PATH = DATA_DIR / "acacia.pid"
SETTINGS_PATH = DATA_DIR / "settings.local.json"


def _load_config():
    from acacia.config import load_config

    return load_config(CONFIG_PATH, SETTINGS_PATH)


def _raw_section(name: str) -> dict:
    """Read one config section without validation, for commands that run before host setup."""
    import tomllib

    if not CONFIG_PATH.exists():
        return {}
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")).get(name, {})


def _credentials_backend():
    from acacia.config import CredentialsConfig

    return CredentialsConfig(**_raw_section("credentials"))


def cmd_credentials(args) -> int:
    from acacia import credentials as c

    conf = _credentials_backend()
    if args.action == "set":
        if conf.backend != "keychain":
            print("credentials.backend is 'file'. Edit that file directly; see the README.")
            return 1
        print("Enter the Tapo Camera Account for this camera. Typing is hidden on purpose.")
        username = getpass.getpass("Camera account username: ")
        password = getpass.getpass("Camera account password: ")
        again = getpass.getpass("Password again: ")
        if not username or not password:
            print("Nothing stored: username and password are both required.")
            return 1
        if password != again:
            print("Nothing stored: the two passwords did not match.")
            return 1
        c.store_keychain(conf.service, username, password)
        print(f"Stored in the macOS Keychain under service '{conf.service}'.")
        print(f"Username length {len(username)}, password length {len(password)}.")
        return 0
    if args.action == "delete":
        removed = c.delete_keychain(conf.service)
        print(f"Removed {removed} Keychain item(s) for service '{conf.service}'.")
        return 0
    try:
        c.load_credentials(conf.backend, conf.service, ROOT / conf.file)
    except c.CredentialError as exc:
        print(f"Not available ({conf.backend}): {exc}")
        return 1
    print(f"Available from {conf.backend}. Values are not displayed.")
    return 0


def _paths():
    from acacia.pipeline import Paths

    return Paths(ROOT, DATA_DIR, MODELS_DIR)


def _configure_logging(cfg) -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    from acacia.sanitize import ScrubFilter

    DATA_DIR.mkdir(exist_ok=True)
    os.chmod(DATA_DIR, 0o700)  # rotated logs and the PID file inherit no looser access
    handlers = [
        logging.StreamHandler(sys.stderr),
        RotatingFileHandler(DATA_DIR / "acacia.log", maxBytes=cfg.retention.log_max_bytes,
                            backupCount=cfg.retention.log_backups, encoding="utf-8"),
    ]
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(ScrubFilter())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    os.chmod(DATA_DIR / "acacia.log", 0o600)


def _running_pid() -> int | None:
    import psutil

    if not PID_PATH.exists():
        return None
    try:
        pid = int(PID_PATH.read_text().strip())
        if "acacia" in " ".join(psutil.Process(pid).cmdline()):
            return pid
    except (ValueError, psutil.Error):
        pass
    PID_PATH.unlink(missing_ok=True)
    return None


def detector_factory(cfg):
    from acacia.detector import MODELS, DetectorError, YoloxOnnxDetector

    spec = MODELS.get(cfg.detector.model)
    if spec is None:
        raise DetectorError(f"Unknown model {cfg.detector.model}. See ./acacia model list")
    return YoloxOnnxDetector(spec, MODELS_DIR, cfg.detector.provider, cfg.detector.nms_iou)


def cmd_run(_args) -> int:
    import uvicorn

    from acacia.pipeline import Pipeline
    from acacia.web.app import create_app

    if pid := _running_pid():
        print(f"Already running (pid {pid}). Use ./acacia stop first.")
        return 1
    cfg = _load_config()
    _configure_logging(cfg)
    pipeline = Pipeline(cfg, _paths(), detector_factory)
    app = create_app(pipeline, cfg)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.server.host, port=cfg.server.port, log_config=None, access_log=False,
        ws="websockets-sansio", timeout_graceful_shutdown=2))
    PID_PATH.write_text(str(os.getpid()))
    print(f"Acacia running at http://{cfg.server.host}:{cfg.server.port}  (Ctrl-C or ./acacia stop)")
    try:
        server.run()
    finally:
        PID_PATH.unlink(missing_ok=True)
    return 0


def cmd_stop(_args) -> int:
    pid = _running_pid()
    if pid is None:
        print("Not running.")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if _running_pid() is None:
            print("Stopped.")
            return 0
        time.sleep(0.2)
    print(f"Process {pid} did not exit in time.")
    return 1


def cmd_status(_args) -> int:
    import json
    import urllib.error
    import urllib.request

    pid = _running_pid()
    if pid is None:
        print("Not running.")
        return 1
    cfg = _load_config()
    base = f"http://{cfg.server.host}:{cfg.server.port}"
    try:
        state = json.load(urllib.request.urlopen(f"{base}/api/state", timeout=3))
        health = json.load(urllib.request.urlopen(f"{base}/api/health", timeout=3))
    except urllib.error.URLError as exc:
        print(f"Running (pid {pid}) but the API did not answer: {scrub(exc)}")
        return 1
    print(json.dumps({"pid": pid, "state": state["state"], "calibration": state["calibration"],
                      "health": health}, indent=2))
    return 0


def cmd_model(args) -> int:
    from acacia.detector import MODELS, sha256_file

    if args.action == "list":
        for spec in MODELS.values():
            path = MODELS_DIR / spec.filename
            present = "present" if path.exists() else "missing"
            print(f"{spec.name:11s} {spec.input_size}px {spec.size_bytes / 1e6:6.1f} MB  {present}")
        return 0

    import urllib.request

    from acacia.config import DetectorConfig

    name = args.name or _raw_section("detector").get("model", DetectorConfig.model)
    if name not in MODELS:
        print(f"Unknown model {name}. Choose from: {', '.join(MODELS)}")
        return 1
    spec = MODELS[name]
    target = MODELS_DIR / spec.filename
    MODELS_DIR.mkdir(exist_ok=True)
    if target.exists() and sha256_file(target) == spec.sha256:
        print(f"{spec.filename} already present and verified.")
        return 0
    tmp = target.with_suffix(".part")
    print(f"Downloading {spec.url}")
    with urllib.request.urlopen(spec.url, timeout=60) as response, tmp.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    if sha256_file(tmp) != spec.sha256:
        tmp.unlink()
        print("Download failed its SHA-256 check and was deleted.")
        return 1
    tmp.replace(target)
    print(f"Verified and saved {target.name}.")
    return 0


def cmd_probe(args) -> int:
    import json

    from acacia.probe import run_probe

    cfg = _load_config()
    report = run_probe(cfg, ROOT, args.stream or cfg.camera.path, args.seconds, args.transport or cfg.camera.transport)
    print(scrub(json.dumps(report, indent=2)))
    return 0 if report.get("frames") else 1


def cmd_bench(args) -> int:
    import platform
    import statistics

    import numpy as np

    from acacia.detector import MODELS, DetectorError, YoloxOnnxDetector

    frame = np.random.default_rng(0).integers(0, 255, (args.height, args.width, 3), dtype=np.uint8)
    print(f"{platform.machine()} {platform.mac_ver()[0]}, frame {args.width}x{args.height}, {args.runs} runs")
    print("Synthetic noise: this measures speed only and says nothing about accuracy.")
    for name, spec in MODELS.items():
        if not (MODELS_DIR / spec.filename).exists():
            continue
        for provider in ("cpu", "coreml"):
            try:
                detector = YoloxOnnxDetector(spec, MODELS_DIR, provider)
            except DetectorError as exc:
                print(f"{name:11s} {provider:6s} unavailable: {exc}")
                continue
            for _ in range(3):
                detector.detect(frame, {"dog": 0.5})
            times = []
            for _ in range(args.runs):
                result = detector.detect(frame, {"dog": 0.5})
                times.append(result.preprocess_ms + result.inference_ms + result.postprocess_ms)
            print(f"{name:11s} {provider:6s} median {statistics.median(times):7.1f} ms  max {max(times):7.1f} ms")
    return 0


def cmd_score(args) -> int:
    from datetime import datetime

    from acacia.scoring import format_report, score_database

    def parse(value):
        return datetime.fromisoformat(value).timestamp() if value else None

    report = score_database(DATA_DIR / "acacia.sqlite3", parse(args.since), parse(args.until), args.pre, args.post)
    print(format_report(report))
    return 0


def cmd_replay(args) -> int:
    from acacia.geometry import load_calibration
    from acacia.replay import format_report, replay_clip

    cfg = _load_config()
    detector = detector_factory(cfg)
    calibration = load_calibration(_paths().calibration)
    try:
        report = replay_clip(Path(args.clip), detector, cfg, calibration, args.interval)
    finally:
        detector.close()
    print(format_report(report, cfg, args.bucket))
    return 0


LWA_CLIENT_ID_PREFIX = "amzn1.application-oa2-client."


def push_cue_secret(value: str, run=None) -> str | None:
    """Store the trigger secret in Parameter Store. Returns an error message, or None on success."""
    return put_secure_parameter("/acacia/trigger_secret", value, run)


def put_secure_parameter(name: str, value: str, run=None) -> str | None:
    """Store one SecureString in Parameter Store. Returns an error message, or None on success.

    The value must not appear on screen or in a process listing, so it cannot be an
    argument. The AWS CLI parses its arguments twice, so it cannot read the request
    from a pipe either: the second read finds the pipe empty. The request therefore
    goes through a file that only this user can read, removed as soon as the CLI exits.
    """
    import json
    import subprocess
    import tempfile

    from acacia.sanitize import register_secret

    register_secret(value)
    run = run or subprocess.run
    request = json.dumps({"Name": name, "Type": "SecureString", "Value": value, "Overwrite": True})
    folder = tempfile.mkdtemp(prefix="acacia-")  # created with mode 0700
    path = os.path.join(folder, "request.json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(request)
        pushed = run(["aws", "ssm", "put-parameter", "--region", "us-east-1",
                      "--cli-input-json", f"file://{path}"],
                     capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return "The aws command was not found."
    except subprocess.TimeoutExpired:
        return "AWS did not answer within 60 seconds."
    finally:
        if os.path.exists(path):
            os.remove(path)
        os.rmdir(folder)
    if pushed.returncode != 0:
        detail = (pushed.stderr or "").strip() or f"aws exited with status {pushed.returncode}"
        return scrub(detail)[-600:]
    return None


def check_alexa_pair(client_id: str, client_secret: str, post=None) -> str | None:
    """Ask Amazon whether this is the skill's Alexa Skill Messaging pair. Returns a problem, or None.

    Only that pair may request the alexa:skill_messaging scope. The Login with Amazon
    security profile's pair authenticates but is refused that scope, which is how the
    two are told apart. Nothing returned here contains either value.
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    def default_post(form: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(
            "https://api.amazon.com/auth/o2/token", data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, reply.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    form = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "alexa:skill_messaging",
                                   "client_id": client_id, "client_secret": client_secret}).encode()
    try:
        status, body = (post or default_post)(form)
    except (OSError, ValueError):
        return "Could not reach Amazon to check the pair."
    if status == 200:
        return None
    try:
        error = json.loads(body).get("error", "")
    except (ValueError, AttributeError):
        error = ""
    if error == "invalid_scope":
        return ("That is the Login with Amazon security profile's pair. Use the pair under "
                "Alexa Skill Messaging on the skill's Permissions page.")
    if error == "invalid_client":
        return "Amazon did not accept that ID and secret together. Check that both came from the same page."
    return f"Amazon refused the pair (status {status}, {error or 'no reason given'})."


def store_alexa_keys(ask, put=None, check=None) -> int:
    """Read the skill's Alexa client ID and secret from hidden prompts and store both in AWS."""
    put = put or put_secure_parameter
    check = check or check_alexa_pair
    print("Paste each value from the Acacia Sensor skill's Permissions page, under Alexa Skill Messaging.")
    print("Not the Login with Amazon security profile: that pair belongs in Account Linking.")
    print("Nothing you paste is shown.")
    client_id = ask("Alexa Client Id: ").strip()
    if not client_id.startswith(LWA_CLIENT_ID_PREFIX):
        print(f"That does not look like an Alexa client ID (it should start with {LWA_CLIENT_ID_PREFIX}).")
        print("Nothing changed.")
        return 1
    client_secret = ask("Alexa Client Secret: ").strip()
    if (len(client_secret) < 32 or any(ch.isspace() for ch in client_secret)
            or client_secret.startswith(LWA_CLIENT_ID_PREFIX) or client_secret == client_id):
        print("That does not look like a client secret. Use Show next to Client Secret, then copy it.")
        print("Nothing changed.")
        return 1
    problem = check(client_id, client_secret)
    if problem:
        print(f"{problem}\nNothing changed.")
        return 1
    for name, value in (("/acacia/lwa_client_id", client_id), ("/acacia/lwa_client_secret", client_secret)):
        error = put(name, value)
        if error:
            print(f"AWS did not store {name}.\n{error}")
            return 1
    print("Stored the Alexa client ID and secret in AWS Parameter Store. Neither was displayed.")
    return 0


def cmd_cue(args) -> int:
    import secrets

    from acacia import credentials as c
    from acacia.cue import CueSendError, HttpSender

    conf = _credentials_backend()
    if args.action == "new-secret":
        value = secrets.token_urlsafe(32)
        error = push_cue_secret(value)
        if error:
            print(f"AWS did not store the secret, so nothing changed.\n{error}")
            return 1
        c.store_cue_secret(conf.service, value)
        print("New cue secret stored in the Keychain and in AWS Parameter Store. It was not displayed.")
        return 0
    if args.action == "alexa-keys":
        return store_alexa_keys(getpass.getpass)

    cfg = _load_config()
    if not cfg.cue.enabled:
        print("The cue is off. Set cue.enabled = true and cue.endpoint in config.toml.")
        return 1
    sender = HttpSender(cfg.cue.endpoint, lambda: c.load_cue_secret(conf.service))
    try:
        if args.action == "check":
            print(sender.request("ping"))
            return 0
        print("This flips the Alexa sensor, so the routine will run and the clip will play in your home.")
        if input("Type play to continue: ").strip() != "play":
            print("Cancelled.")
            return 1
        print(sender.request("detected"))
        print("Wait at least 35 seconds, then run: ./acacia cue reset")
        return 0
    except (CueSendError, c.CredentialError) as exc:
        print(f"Failed: {scrub(exc)}")
        return 1


def cmd_cue_reset(_args) -> int:
    from acacia import credentials as c
    from acacia.cue import CueSendError, HttpSender

    cfg = _load_config()
    conf = _credentials_backend()
    try:
        print(HttpSender(cfg.cue.endpoint, lambda: c.load_cue_secret(conf.service)).request("clear"))
        return 0
    except (CueSendError, c.CredentialError) as exc:
        print(f"Failed: {scrub(exc)}")
        return 1


def cmd_check_secrets(_args) -> int:
    from acacia.secret_scan import scan

    conf = _credentials_backend()
    findings = scan(ROOT, conf)
    for finding in findings:
        print(finding)
    print("Secret scan: clean." if not findings else f"Secret scan: {len(findings)} finding(s).")
    return 1 if findings else 0


def main(argv: list[str] | None = None) -> int:
    install_exception_hooks()
    parser = argparse.ArgumentParser(prog="acacia", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("credentials", help="store or check camera credentials locally")
    p.add_argument("action", choices=["set", "status", "delete"])
    p.set_defaults(func=cmd_credentials)

    sub.add_parser("run", help="start the app in this terminal").set_defaults(func=cmd_run)
    sub.add_parser("stop", help="stop a running app").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="print current state and health").set_defaults(func=cmd_status)

    p = sub.add_parser("model", help="list or download verified model files")
    p.add_argument("action", choices=["list", "fetch"])
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("probe", help="measure the real stream's properties")
    p.add_argument("--stream", help="stream path, for example /stream1")
    p.add_argument("--seconds", type=float, default=15.0)
    p.add_argument("--transport", choices=["tcp", "udp"])
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("bench", help="measure detector speed on this machine")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--runs", type=int, default=30)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("score", help="score confirmed events against ground-truth marks")
    p.add_argument("--since", help="ISO time, for example 2026-11-08T09:00")
    p.add_argument("--until", help="ISO time")
    p.add_argument("--pre", type=float, default=5.0, help="seconds an event may precede an on_start mark")
    p.add_argument("--post", type=float, default=10.0, help="seconds an event may follow an on_end mark")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("replay", help="run a saved clip through detection and, if calibrated, the state machine")
    p.add_argument("clip")
    p.add_argument("--interval", type=float, default=0.25, help="seconds of video between inferences")
    p.add_argument("--bucket", type=float, default=15.0, help="seconds per row in the timeline")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("cue", help="set up or test the optional voice cue")
    p.add_argument("action", choices=["new-secret", "alexa-keys", "check", "test", "reset"])
    p.set_defaults(func=lambda args: cmd_cue_reset(args) if args.action == "reset" else cmd_cue(args))

    sub.add_parser("check-secrets", help="scan the repo for credentials").set_defaults(func=cmd_check_secrets)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort sanitized report
        print(f"error: {scrub(exc)}", file=sys.stderr)
        return 1
