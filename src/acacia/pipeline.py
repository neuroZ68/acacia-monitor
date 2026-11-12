"""Wires ingest, inference, and decisions together as three isolated threads.

Decisions run on their own timer, so a hung decoder or model cannot also hang
the watchdog: the state still drops to UNKNOWN.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from acacia import __version__
from acacia.classifier import FrameEvidence, classify_frame
from acacia.config import Config, apply_overrides, atomic_write_text, tunable_values, validate
from acacia.credentials import RtspUrl, load_cue_secret, load_credentials
from acacia.cue import CueController, HttpSender, Sender
from acacia.detector import Detection, DetectionResult, Detector
from acacia.events import EventStore
from acacia.geometry import (
    Calibration,
    CalibrationError,
    CushionMask,
    View,
    load_calibration,
    parse_polygons,
    save_calibration,
)
from acacia.health import Metrics
from acacia.ingest import Frame, IngestStatus, IngestWorker, LatestSlot, PyAvSource
from acacia.sanitize import scrub
from acacia.state import CouchStateMachine, StateEvent

log = logging.getLogger("acacia.pipeline")

DECISION_TICK_S = 0.1
SCENE_SAMPLE_STRIDE = 16
# Infrared night view is grey, so its channels nearly match. This value is a
# hypothesis to check against a real day-to-night transition.
IR_CHROMA_MAX = 3.0


def scene_stats(rgb: np.ndarray) -> tuple[float, float]:
    """Mean brightness and colourfulness: numbers only, so no image leaves this function."""
    sample = rgb[::SCENE_SAMPLE_STRIDE, ::SCENE_SAMPLE_STRIDE].astype(np.int16)
    red, green, blue = sample[..., 0], sample[..., 1], sample[..., 2]
    chroma = (np.abs(red - green) + np.abs(green - blue)).mean() / 2
    return float(sample.mean()), float(chroma)


def class_thresholds(cfg: Config) -> dict[str, float]:
    det = cfg.detector
    thresholds = {"dog": det.min_confidence, "person": det.person_confidence}
    for label in det.context_classes:
        thresholds[label] = det.min_confidence
    return thresholds


def view_mode(chroma: float) -> str:
    return "infrared" if chroma <= IR_CHROMA_MAX else "colour"


@dataclass(frozen=True)
class Paths:
    root: Path
    data: Path
    models: Path

    @property
    def calibration(self) -> Path:
        return self.data / "calibration.json"

    @property
    def database(self) -> Path:
        return self.data / "acacia.sqlite3"

    @property
    def settings(self) -> Path:
        return self.data / "settings.local.json"


@dataclass(frozen=True)
class InferenceOutcome:
    frame_seq: int
    session: int
    width: int
    height: int
    t_decoded: float
    backlog_s: float
    t_started: float
    t_finished: float
    result: DetectionResult | None
    error: str = ""
    luma: float = 0.0
    chroma: float = 0.0


class Pipeline:
    def __init__(self, cfg: Config, paths: Paths, detector_factory: Callable[[Config], Detector],
                 source_factory=PyAvSource, cue_sender: Sender | None = None):
        self.cfg = cfg
        self.paths = paths
        self.run_id = uuid.uuid4().hex[:8]
        self.detector_factory = detector_factory
        self.source_factory = source_factory

        self.metrics = Metrics()
        self.ingest_status = IngestStatus()
        self.frames: LatestSlot[Frame] = LatestSlot()
        self.outcomes: LatestSlot[InferenceOutcome] = LatestSlot()
        self.store = EventStore(paths.database, cfg.retention.max_events, cfg.retention.max_age_days, self.run_id)
        self.machine = CouchStateMachine(cfg.state)
        self.calibration = load_calibration(paths.calibration)

        self._lock = threading.RLock()  # guards calibration, cfg, and the published snapshot
        self._mask: CushionMask | None = None
        self._mask_key: tuple | None = None
        self._live_view: View | None = None
        self._evidence: FrameEvidence | None = None
        self._outcome: InferenceOutcome | None = None
        self._latest: InferenceOutcome | None = None  # newest inference, classified or not
        self._evidence_at: float | None = None
        self._stop = threading.Event()
        self._last_inference_ok: float | None = None
        self._fresh_since: float | None = None
        self._last_session = 0
        self._detector: Detector | None = None
        self._detector_error = ""
        self._threads: list[threading.Thread] = []
        self._ingest: IngestWorker | None = None
        self._last_metrics_at = 0.0
        self._last_person_at: float | None = None
        if cue_sender is None and cfg.cue.enabled:
            cue_sender = HttpSender(cfg.cue.endpoint, lambda: load_cue_secret(cfg.credentials.service))
        self.cue = CueController(cfg.cue, self._on_cue_event, cue_sender)
        self._outcome_version = 0
        self._decided: OrderedDict[int, float] = OrderedDict()  # frame seq -> decision time

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.store.add_event("system", "app_started", reason=f"run {self.run_id}", detail={
            "version": __version__,
            "model": self.cfg.detector.model,
            "provider_setting": self.cfg.detector.provider,
            "stream": self.cfg.camera.path,
            "settings": tunable_values(self.cfg),
        })
        self._ingest = IngestWorker(self.cfg.camera, self._url, self.frames, self.ingest_status,
                                    self._on_frame, self.source_factory, self._on_ingest_event)
        inference = threading.Thread(target=self._inference_loop, name="inference", daemon=True)
        decision = threading.Thread(target=self._decision_loop, name="decision", daemon=True)
        self._threads = [self._ingest, inference, decision]
        for thread in self._threads:
            thread.start()
        self.cue.start()

    def stop(self) -> None:
        self.cue.set_armed(False)
        self.cue.stop()
        self._stop.set()
        if self._ingest:
            self._ingest.stop()
        deadline = time.monotonic() + self.cfg.camera.read_timeout_s + 2.0
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        stuck = [t.name for t in self._threads if t.is_alive()]
        if stuck:
            log.warning("threads still alive at stop: %s", ", ".join(stuck))
        if self._detector:
            self._detector.close()
        self.frames.clear()
        self.store.add_event("system", "app_stopped", reason=f"run {self.run_id}")
        self.store.close()

    def _url(self) -> RtspUrl:
        cam, conf = self.cfg.camera, self.cfg.credentials
        creds = load_credentials(conf.backend, conf.service, self.paths.root / conf.file)
        return RtspUrl(cam.host, cam.port, cam.path, creds)

    def retry_ingest(self) -> None:
        if self._ingest:
            self._ingest.retry()

    def _on_frame(self, frame: Frame) -> None:
        self.metrics.decoded.tick(frame.t_decoded)

    def _on_cue_event(self, kind: str, reason: str, detail: dict) -> None:
        self.store.add_event("cue", kind, reason=reason, detail=detail)

    def _on_ingest_event(self, kind: str, reason: str) -> None:
        self.store.add_event("system", kind, reason=scrub(reason))

    # -- inference thread --------------------------------------------------

    def _ensure_detector(self) -> Detector | None:
        if self._detector is None:
            try:
                self._detector = self.detector_factory(self.cfg)
                self._detector_error = ""
            except Exception as exc:  # noqa: BLE001 - surfaced through health
                message = scrub(exc)
                if message != self._detector_error:
                    log.error("detector failed to load: %s", message)
                self._detector_error = message
        return self._detector

    def _inference_loop(self) -> None:
        version = 0
        next_due = 0.0
        while not self._stop.is_set():
            # Pace first, then take the newest frame, so cadence delay never ages the input.
            wait = next_due - time.monotonic()
            if wait > 0 and self._stop.wait(wait):
                break
            frame, version = self.frames.take_newer(version, 0.5)
            if frame is None:
                continue
            started = time.monotonic()
            age_s = started - frame.t_decoded
            if age_s > self.cfg.state.stale_timeout_s:
                self.metrics.dropped_stale += 1
                continue
            next_due = started + self.cfg.detector.inference_interval_s

            detector = self._ensure_detector()
            if detector is None:
                self._publish_failure(frame, started, self._detector_error)
                self._stop.wait(1.0)
                continue
            try:
                rgb = frame.rgb()
                result = detector.detect(rgb, class_thresholds(self.cfg))
                luma, chroma = scene_stats(rgb)
            except Exception as exc:  # noqa: BLE001 - a failed frame must not kill the worker
                self.metrics.inference_failures += 1
                self._publish_failure(frame, started, scrub(exc))
                continue
            finished = time.monotonic()
            self.metrics.inferred.tick(finished)
            self.metrics.frame_age_at_inference_ms.add(age_s * 1e3)
            self.metrics.inference_ms.add(result.inference_ms)
            self.metrics.preprocess_ms.add(result.preprocess_ms)
            self.outcomes.put(InferenceOutcome(frame.seq, frame.session, frame.width, frame.height,
                                               frame.t_decoded, frame.backlog_s, started, finished, result,
                                               luma=luma, chroma=chroma))

    def _publish_failure(self, frame: Frame, started: float, message: str) -> None:
        self.outcomes.put(InferenceOutcome(frame.seq, frame.session, frame.width, frame.height, frame.t_decoded,
                                           frame.backlog_s, started, time.monotonic(), None, message))

    # -- decision thread ---------------------------------------------------

    def _emit(self, events: list[StateEvent]) -> None:
        latest = self._latest
        for event in events:
            # Tagged so results can be split by lighting without keeping any image.
            detail = {**event.detail, "view_mode": view_mode(latest.chroma) if latest else None}
            self.store.add_event(event.tier, event.kind, event.state_from.value, event.state_to.value,
                                 event.reason, detail)
            log.info("%s %s -> %s %s", event.kind, event.state_from.value, event.state_to.value, event.reason)
            if event.kind == "confirmed_on":
                self.cue.on_confirmed_on(time.monotonic(), datetime.now().astimezone(), self._last_person_at)

    def _fault_reason(self, now: float) -> str | None:
        """Why no decision can be trusted right now, or None if the pipeline is healthy."""
        state_cfg = self.cfg.state
        with self.ingest_status.lock:
            phase = self.ingest_status.phase
            session = self.ingest_status.session
        if phase in ("no_credentials", "auth_failed"):
            return phase

        frame, _ = self.frames.peek()
        if frame is None:
            return "no_video_yet"
        age = now - frame.t_decoded
        if age > state_cfg.stale_timeout_s:
            self._fresh_since = None
            return "stale_stream" if phase == "streaming" else "stream_disconnected"
        if frame.backlog_s > state_cfg.stale_timeout_s:
            self._fresh_since = None
            return "replaying_buffered_frames"

        # Recovery only counts once frames have flowed steadily for a while, so a
        # burst of buffered frames cannot pass as fresh video.
        if session != self._last_session:
            self._last_session = session
            self._fresh_since = None
        if self._fresh_since is None:
            self._fresh_since = now
        if now - self._fresh_since < state_cfg.recovery_warmup_s:
            return "stream_warming_up"

        with self._lock:
            view = View(frame.width, frame.height, self.cfg.camera.rotation_deg, self.cfg.camera.path)
            self._live_view = view
            status, _detail = self.calibration.status(view)
        if status != "valid":
            return f"calibration_{status}"

        if self._detector_error:
            return "detector_unavailable"
        last_ok = self._last_inference_ok
        if last_ok is None:
            return "awaiting_inference"
        if now - last_ok > state_cfg.stale_timeout_s:
            return "inference_failed_or_stalled"
        return None

    def _mask_for(self, width: int, height: int) -> CushionMask:
        with self._lock:
            key = (self.calibration.revision, width, height)
            if self._mask is None or self._mask_key != key:
                self._mask = CushionMask(self.calibration.enabled_polygons(), width, height)
                self._mask_key = key
            return self._mask

    def _decision_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._decide()
                self._sample_metrics()
            except Exception:  # noqa: BLE001 - the watchdog must outlive any single bug
                log.exception("decision tick failed")

    def _decide(self) -> None:
        # Wakes as soon as a result lands, and at least once per tick for the watchdog.
        outcome, self._outcome_version = self.outcomes.take_newer(self._outcome_version, DECISION_TICK_S)
        now = time.monotonic()
        if outcome is not None and outcome.result is not None:
            self._last_inference_ok = now
            self._latest = outcome
            if outcome.result.of("person"):
                self._last_person_at = now
        fault = self._fault_reason(now)
        with self._lock:
            if fault is not None:
                self._emit(self.machine.fault(now, fault))
                self._evidence = None
                return
            if outcome is None or outcome.result is None:
                return
            # Evidence from before the last reconnect is not fresh.
            if outcome.session != self._last_session:
                return
            cfg = self.cfg
            mask = self._mask_for(outcome.width, outcome.height)
            evidence = classify_frame(outcome.result.of("dog"), outcome.result.of("person"), mask,
                                      cfg.classifier, cfg.detector.dog_confidence)
            self._emit(self.machine.observe(outcome.t_decoded, evidence.evidence, evidence.visible_fraction,
                                            cfg.classifier.lost_min_visible_fraction))
            self._evidence = evidence
            self._outcome = outcome
            self._evidence_at = now
        decided = time.monotonic()
        with self._lock:
            self._decided[outcome.frame_seq] = decided
            while len(self._decided) > 64:
                self._decided.popitem(last=False)
        self.metrics.decisions.tick(decided)
        self.metrics.decode_to_decision_ms.add((decided - outcome.t_decoded) * 1e3)

    def _sample_metrics(self) -> None:
        now = time.monotonic()
        if now - self._last_metrics_at < self.cfg.retention.metrics_interval_s:
            return
        self._last_metrics_at = now
        self.store.add_metrics({
            "timing": self.metrics.timing_json(),
            "resources": self.metrics.resources_json(),
            "dropped_frames": self.frames.overwritten,
            "reconnects": self.ingest_status.reconnects,
            "state": self.machine.state.value,
            "view_mode": view_mode(self._latest.chroma) if self._latest else None,
        })

    # -- calibration and settings -----------------------------------------

    def save_polygons(self, raw_polygons: object) -> dict:
        polygons = parse_polygons(raw_polygons)
        with self._lock:
            live = self._live_view
            if live is None and polygons:
                raise CalibrationError("Calibrate against live video: no frame has arrived yet")
            self.calibration.polygons = polygons
            self.calibration.view = live
            self.calibration.invalidated = False
            self.calibration.invalid_reason = ""
            save_calibration(self.paths.calibration, self.calibration)
            revision = self.calibration.revision
        self.store.add_event("system", "calibration_saved", reason=f"revision {revision}",
                             detail={"polygons": len(polygons)})
        return self.calibration_json()

    def invalidate_calibration(self, reason: str) -> dict:
        with self._lock:
            self.calibration.invalidated = True
            self.calibration.invalid_reason = reason[:200] or "marked by user"
            save_calibration(self.paths.calibration, self.calibration)
        self.store.add_event("system", "calibration_invalidated", reason=reason[:200])
        return self.calibration_json()

    def calibration_json(self) -> dict:
        with self._lock:
            status, detail = self.calibration.status(self._live_view)
            data = self.calibration.to_json()
        data["status"] = status
        data["detail"] = detail
        return data

    def settings_json(self) -> dict:
        return tunable_values(self.cfg)

    def update_settings(self, overrides: dict) -> dict:
        with self._lock:
            updated = apply_overrides(self.cfg, overrides)
            validate(updated)
            self.cfg = updated
            self.machine.cfg = updated.state
            stored = {}
            if self.paths.settings.exists():
                stored = json.loads(self.paths.settings.read_text(encoding="utf-8"))
            stored.update(overrides)
            atomic_write_text(self.paths.settings, json.dumps(stored, indent=2, sort_keys=True))
            self.store.max_events = updated.retention.max_events
            self.store.max_age_days = updated.retention.max_age_days
        self.store.add_event("system", "settings_changed", detail=overrides)
        return self.settings_json()

    # -- published state ---------------------------------------------------

    def note_ui_ack(self, frame_seq: object, acked_at: float) -> None:
        """Record decision-to-display time, once, for the first ack of a decision."""
        with self._lock:
            decided = self._decided.pop(frame_seq, None) if isinstance(frame_seq, int) else None
        if decided is not None:
            self.metrics.decision_to_ui_ack_ms.add((acked_at - decided) * 1e3)

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            machine = self.machine.snapshot(now)
            evidence, outcome, evidence_at = self._evidence, self._outcome, self._evidence_at
            latest = self._latest
            calibration_status, calibration_detail = self.calibration.status(self._live_view)
            revision = self.calibration.revision
        frame, _ = self.frames.peek()
        # Detections stay visible without a classification, for example before
        # calibration, so the overlay shows what the model sees at any time.
        fresh = latest is not None and now - latest.t_finished <= self.cfg.state.stale_timeout_s
        result = latest.result if fresh else None
        return {
            "state": machine,
            "evidence": evidence.to_json() if evidence else None,
            "evidence_age_s": None if evidence_at is None or evidence is None else round(now - evidence_at, 2),
            "evidence_frame_seq": outcome.frame_seq if outcome and evidence else None,
            "unclassified_dogs": [_box_json(d) for d in result.of("dog")] if result and not evidence else [],
            "scene": None if not fresh else {"luma": round(latest.luma, 1), "chroma": round(latest.chroma, 1),
                                             "mode": view_mode(latest.chroma)},
            "persons": [_box_json(d) for d in result.of("person")] if result else [],
            "context": [_box_json(d) for d in result.detections if d.label not in ("dog", "person")] if result else [],
            "calibration": {"status": calibration_status, "detail": calibration_detail, "revision": revision},
            "frame": None if frame is None else {
                "seq": frame.seq, "width": frame.width, "height": frame.height,
                "age_ms": round((now - frame.t_decoded) * 1e3),
                "backlog_ms": round(frame.backlog_s * 1e3),
            },
        }

    def health(self) -> dict:
        detector = self._detector
        ingest = self.ingest_status.to_json()
        stream = ingest["stream"]
        return {
            "run_id": self.run_id,
            "ingest": ingest,
            "dropped_frames": self.frames.overwritten,
            "timing": self.metrics.timing_json(),
            "resources": self.metrics.resources_json(),
            "detector": {
                "model": detector.name if detector else self.cfg.detector.model,
                "provider": detector.provider if detector else None,
                "error": self._detector_error,
            },
            # Read from the open stream, so a regression would show up here.
            "cue": self.cue.status(),
            "privacy": {"audio_ingest": bool(stream and stream["audio_tracks_opened"]), "image_retention": False,
                        "bind": f"{self.cfg.server.host}:{self.cfg.server.port}"},
        }


def _box_json(detection: Detection) -> dict:
    return {"label": detection.label, "confidence": round(detection.confidence, 3),
            "box": [round(v, 4) for v in detection.box]}

