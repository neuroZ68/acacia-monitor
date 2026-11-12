"""Replay a saved clip through detection, classification, and the state machine.

Video timestamps act as the clock, so a clip behaves as it would have live and
results are repeatable after tuning. Reads video only, ignores any audio track,
and writes nothing. The numbers it prints are the whole output.

A clip records one camera position, so polygons only apply if the camera has not
moved since it was recorded. The report states which calibration was used.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import av

from acacia.classifier import classify_frame
from acacia.config import Config
from acacia.detector import Detector
from acacia.geometry import Calibration, CushionMask, View
from acacia.ingest import Frame
from acacia.pipeline import class_thresholds, scene_stats, view_mode
from acacia.state import CouchStateMachine, StateEvent


@dataclass
class Sample:
    t: float
    dog_confidence: float | None  # the most confident dog box in this sample
    dog_box: tuple | None
    persons: int
    context: tuple[str, ...]
    evidence: str | None


@dataclass
class ClipReport:
    path: str
    width: int
    height: int
    duration_s: float
    interval_s: float
    calibration_note: str
    samples: list[Sample] = field(default_factory=list)
    events: list[tuple[float, StateEvent]] = field(default_factory=list)
    final_state: str | None = None
    view_modes: Counter = field(default_factory=Counter)


def replay_clip(path: Path, detector: Detector, cfg: Config, calibration: Calibration,
                interval_s: float = 0.25) -> ClipReport:
    if not path.exists():
        raise FileNotFoundError(f"No such clip: {path.name}")
    thresholds = class_thresholds(cfg)
    rotation = cfg.camera.rotation_deg
    mask: CushionMask | None = None
    machine: CouchStateMachine | None = None
    report: ClipReport | None = None

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"  # replay wants throughput, not low delay
        due = 0.0
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                continue
            t = float(frame.pts * frame.time_base)
            if t < due:
                continue
            due = t + interval_s
            image = Frame(0, 0, frame, frame.width, frame.height, 0.0, 0.0, rotation)
            if report is None:
                live = View(image.width, image.height, rotation, cfg.camera.path)
                status, detail = calibration.status(live)
                note = f"calibration {status}: {detail}"
                if status == "valid":
                    mask = CushionMask(calibration.enabled_polygons(), image.width, image.height)
                    machine = CouchStateMachine(cfg.state)
                    note = f"calibration revision {calibration.revision}, saved {calibration.saved_at}"
                seconds = float(stream.duration * stream.time_base) if stream.duration else 0.0
                report = ClipReport(path.name, image.width, image.height, seconds, interval_s, note)

            rgb = image.rgb()
            result = detector.detect(rgb, thresholds)
            report.view_modes[view_mode(scene_stats(rgb)[1])] += 1
            dogs, people = result.of("dog"), result.of("person")
            best = max(dogs, key=lambda d: d.confidence, default=None)
            evidence = None
            if mask is not None and machine is not None:
                frame_evidence = classify_frame(dogs, people, mask, cfg.classifier, cfg.detector.dog_confidence)
                evidence = frame_evidence.evidence.value
                for event in machine.observe(t, frame_evidence.evidence, frame_evidence.visible_fraction,
                                             cfg.classifier.lost_min_visible_fraction):
                    report.events.append((t, event))
            report.samples.append(Sample(
                t, best.confidence if best else None, best.box if best else None, len(people),
                tuple(d.label for d in result.detections if d.label not in ("dog", "person")), evidence))
    if report is None:
        raise ValueError(f"No decodable video in {path.name}")
    report.final_state = machine.state.value if machine else None
    return report


def _gaps(times: list[float], start: float, end: float) -> list[float]:
    """Time between consecutive sightings, including the clip's opening and closing stretches."""
    points = [start, *times, end]
    return [b - a for a, b in zip(points, points[1:])]


def format_report(report: ClipReport, cfg: Config, bucket_s: float = 15.0) -> str:
    samples = report.samples
    threshold = cfg.detector.dog_confidence
    confident = [s for s in samples if s.dog_confidence is not None and s.dog_confidence >= threshold]
    weak = [s for s in samples if s.dog_confidence is not None and s.dog_confidence < threshold]
    start, end = samples[0].t, samples[-1].t
    lines = [
        f"Clip: {report.path}, {report.width}x{report.height}, {report.duration_s:.0f} s, "
        f"{len(samples)} samples every {report.interval_s} s",
        f"View mode by sample: {dict(report.view_modes)}",
        f"Polygons: {report.calibration_note}",
        f"Dog at or above {threshold}: {len(confident)} of {len(samples)} samples "
        f"({len(confident) / len(samples) * 100:.1f}%)",
        f"Dog below {threshold}: {len(weak)} samples",
        f"Person in view: {sum(1 for s in samples if s.persons)} samples",
    ]
    if confident:
        values = sorted(s.dog_confidence for s in confident)
        lines.append(f"Dog confidence: median {median(values):.2f}, range {values[0]:.2f} to {values[-1]:.2f}")
        gaps = _gaps([s.t for s in confident], start, end)
        over = [g for g in gaps if g > cfg.state.evidence_gap_s]
        lines.append(f"Gaps between confident sightings: longest {max(gaps):.1f} s, "
                     f"{len(over)} longer than evidence_gap_s ({cfg.state.evidence_gap_s} s)")
        hold = [g for g in gaps if g > cfg.state.hold_s]
        lines.append(f"Gaps longer than hold_s ({cfg.state.hold_s} s): {len(hold)}")
    context = Counter(label for s in samples for label in s.context)
    if context:
        lines.append(f"Other labels (context only): {dict(context.most_common())}")

    lines.append("")
    lines.append(f"Timeline, {bucket_s:.0f} s rows. Dog box values are normalized: center x, bottom y, height.")
    lines.append("  time        dog%  median  center  bottom  height  people  evidence")
    t = start
    while t <= end:
        rows = [s for s in samples if t <= s.t < t + bucket_s]
        seen = [s for s in rows if s.dog_confidence is not None and s.dog_confidence >= threshold]
        if rows:
            percent = len(seen) / len(rows) * 100
            if seen:
                center = median((s.dog_box[0] + s.dog_box[2]) / 2 for s in seen)
                bottom = median(s.dog_box[3] for s in seen)
                height = median(s.dog_box[3] - s.dog_box[1] for s in seen)
                stats = (f"{median(s.dog_confidence for s in seen):6.2f}  {center:6.2f}  {bottom:6.2f}  {height:6.2f}")
            else:
                stats = f"{'-':>6}  {'-':>6}  {'-':>6}  {'-':>6}"
            people = median(s.persons for s in rows)
            evidence = Counter(s.evidence for s in rows if s.evidence).most_common(1)
            lines.append(f"  {t:4.0f}-{t + bucket_s:<4.0f} {percent:5.0f}  {stats}  {people:6.0f}  "
                         f"{evidence[0][0] if evidence else 'n/a'}")
        t += bucket_s

    if report.events:
        lines.append("")
        lines.append("State machine events, in video time:")
        for when, event in report.events:
            arrow = f"{event.state_from.value} to {event.state_to.value}"
            lines.append(f"  {when:6.1f} s  {event.tier:9s} {event.kind} {arrow} {event.reason}".rstrip())
        lines.append(f"Final state: {report.final_state}")
    lines.append("")
    lines.append("One clip cannot establish an accuracy figure. See docs/VALIDATION.md.")
    return "\n".join(lines)
