"""Cushion polygons and calibration records.

Polygons are stored in normalized [0, 1] coordinates with the view they were
drawn against, so display scaling never changes them, while a changed crop,
rotation, or stream invalidates them.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from acacia.config import atomic_write_text

MAX_POLYGONS = 32
MAX_POINTS = 64
MIN_AREA = 1e-4  # 0.01 percent of the frame
MASK_MAX_WIDTH = 640
ASPECT_TOLERANCE = 0.01


class CalibrationError(Exception):
    pass


def polygon_area(points: list[list[float]]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, c) -> bool:
    return (min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))


def _segments_intersect(p1, p2, p3, p4) -> bool:
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and 0 not in (d1, d2, d3, d4):
        return True
    return ((d1 == 0 and _on_segment(p3, p4, p1)) or (d2 == 0 and _on_segment(p3, p4, p2))
            or (d3 == 0 and _on_segment(p1, p2, p3)) or (d4 == 0 and _on_segment(p1, p2, p4)))


def is_self_intersecting(points: list[list[float]]) -> bool:
    n = len(points)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # adjacent edges share a vertex
            if _segments_intersect(points[i], points[(i + 1) % n], points[j], points[(j + 1) % n]):
                return True
    return False


def validate_points(points: object, label: str = "polygon") -> list[list[float]]:
    if not isinstance(points, list) or not 3 <= len(points) <= MAX_POINTS:
        raise CalibrationError(f"{label}: needs 3 to {MAX_POINTS} points")
    clean: list[list[float]] = []
    for point in points:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in point)):
            raise CalibrationError(f"{label}: each point must be [x, y]")
        x, y = float(point[0]), float(point[1])
        if not (math.isfinite(x) and math.isfinite(y)) or not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise CalibrationError(f"{label}: points must lie inside the frame")
        clean.append([x, y])
    if len({(x, y) for x, y in clean}) != len(clean):
        raise CalibrationError(f"{label}: duplicate points")
    if polygon_area(clean) < MIN_AREA:
        raise CalibrationError(f"{label}: shape is degenerate or too small")
    if is_self_intersecting(clean):
        raise CalibrationError(f"{label}: edges cross each other")
    return clean


@dataclass(frozen=True)
class View:
    """The assumptions a calibration is tied to."""

    source_width: int
    source_height: int
    rotation_deg: int
    stream_path: str
    crop: None = None  # phase 1 applies no crop; recorded so the assumption is explicit

    @property
    def aspect(self) -> float:
        return self.source_width / self.source_height


@dataclass
class Polygon:
    id: str
    name: str
    enabled: bool
    points: list[list[float]]


@dataclass
class Calibration:
    polygons: list[Polygon] = field(default_factory=list)
    view: View | None = None
    revision: int = 0
    saved_at: str | None = None
    invalidated: bool = False
    invalid_reason: str = ""

    def enabled_polygons(self) -> list[Polygon]:
        return [p for p in self.polygons if p.enabled]

    def status(self, live: View | None) -> tuple[str, str]:
        """Return (status, detail). Only status "valid" may produce a classification."""
        if self.invalidated:
            return "invalid", self.invalid_reason or "recalibration required"
        if not self.enabled_polygons() or self.view is None:
            return "missing", "no enabled cushion polygons"
        if live is None:
            return "unverified", "waiting for live video to compare against"
        if live.rotation_deg != self.view.rotation_deg:
            return "invalid", "camera rotation changed since calibration"
        if live.stream_path != self.view.stream_path:
            return "invalid", "stream changed since calibration"
        if abs(live.aspect - self.view.aspect) > ASPECT_TOLERANCE * self.view.aspect:
            return "invalid", "frame aspect ratio changed since calibration"
        return "valid", "matches the live view; camera movement is not auto-detected"

    def to_json(self) -> dict:
        return {
            "version": 1,
            "revision": self.revision,
            "saved_at": self.saved_at,
            "invalidated": self.invalidated,
            "invalid_reason": self.invalid_reason,
            "view": asdict(self.view) if self.view else None,
            "polygons": [asdict(p) for p in self.polygons],
        }


def parse_polygons(raw: object) -> list[Polygon]:
    if not isinstance(raw, list) or len(raw) > MAX_POLYGONS:
        raise CalibrationError(f"polygons must be a list of at most {MAX_POLYGONS}")
    polygons: list[Polygon] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CalibrationError("each polygon must be an object")
        name = str(item.get("name") or f"cushion {index + 1}")[:60]
        polygons.append(Polygon(
            id=str(item.get("id") or uuid.uuid4().hex[:8])[:32],
            name=name,
            enabled=bool(item.get("enabled", True)),
            points=validate_points(item.get("points"), name),
        ))
    return polygons


def load_calibration(path: Path) -> Calibration:
    if not path.exists():
        return Calibration()
    raw = json.loads(path.read_text(encoding="utf-8"))
    view = raw.get("view")
    return Calibration(
        polygons=parse_polygons(raw.get("polygons", [])),
        view=View(**view) if view else None,
        revision=int(raw.get("revision", 0)),
        saved_at=raw.get("saved_at"),
        invalidated=bool(raw.get("invalidated", False)),
        invalid_reason=str(raw.get("invalid_reason", "")),
    )


def save_calibration(path: Path, calibration: Calibration) -> None:
    calibration.revision += 1
    calibration.saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_text(path, json.dumps(calibration.to_json(), indent=2))


class CushionMask:
    """A rasterized union of enabled polygons, for fast overlap measurements."""

    def __init__(self, polygons: list[Polygon], width: int, height: int):
        scale = min(1.0, MASK_MAX_WIDTH / width)
        self.width = max(1, round(width * scale))
        self.height = max(1, round(height * scale))
        canvas = Image.new("1", (self.width, self.height), 0)
        draw = ImageDraw.Draw(canvas)
        for polygon in polygons:
            draw.polygon([(x * self.width, y * self.height) for x, y in polygon.points], fill=1)
        self.mask = np.asarray(canvas, dtype=bool)
        self.total = int(self.mask.sum())

    def _slice(self, x1: float, y1: float, x2: float, y2: float):
        """Mask pixels under a normalized rectangle, always at least 1 px."""
        ix1 = min(self.width - 1, max(0, int(x1 * self.width)))
        iy1 = min(self.height - 1, max(0, int(y1 * self.height)))
        ix2 = min(self.width, max(ix1 + 1, math.ceil(x2 * self.width)))
        iy2 = min(self.height, max(iy1 + 1, math.ceil(y2 * self.height)))
        return self.mask[iy1:iy2, ix1:ix2]

    def fraction(self, x1: float, y1: float, x2: float, y2: float) -> float:
        region = self._slice(x1, y1, x2, y2)
        return float(region.mean()) if region.size else 0.0

    def contains(self, x: float, y: float) -> bool:
        ix = min(self.width - 1, max(0, int(x * self.width)))
        iy = min(self.height - 1, max(0, int(y * self.height)))
        return bool(self.mask[iy, ix])

    def covered_fraction(self, boxes: list[tuple[float, float, float, float]]) -> float:
        """Fraction of cushion area hidden by the union of boxes."""
        if not self.total or not boxes:
            return 0.0
        hidden = np.zeros_like(self.mask)
        for x1, y1, x2, y2 in boxes:
            ix1, iy1 = int(x1 * self.width), int(y1 * self.height)
            ix2, iy2 = math.ceil(x2 * self.width), math.ceil(y2 * self.height)
            hidden[max(0, iy1):max(0, iy2), max(0, ix1):max(0, ix2)] = True
        return float((hidden & self.mask).sum()) / self.total
