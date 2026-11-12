"""Per-frame couch evidence from detector boxes and cushion geometry.

A detector box is not a mask or a physical support measurement. Perspective can
put a floor dog's box over the cushions, so whole-box overlap alone is weak.
The score leans on where the bottom of the box sits, which is the closest a box
gets to the point of contact, and it stays explicit about when that bottom edge
cannot be trusted (an occluding person, or a box cut off by the frame).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from acacia.config import ClassifierConfig
from acacia.detector import Detection
from acacia.geometry import CushionMask
from acacia.state import Evidence

# Geometry bands, as fractions of box height.
LOWER_BAND = 0.35
FOOT_BAND = 0.10
FOOT_WIDTH = 0.60  # central share of the box width; corners of a box are often empty
FRAME_EDGE = 0.01
NEAR_MARGIN = 0.25

W_BOTTOM, W_LOWER, W_BOX = 0.55, 0.35, 0.10

LABEL_ON = "ON"
LABEL_AMBIGUOUS = "AMBIGUOUS"
LABEL_OFF = "OFF_COUCH"
LABEL_WEAK = "WEAK"


@dataclass(frozen=True)
class DogEvidence:
    box: tuple[float, float, float, float]
    confidence: float
    label: str
    support: float        # geometric support score, independent of model confidence
    bottom_support: float
    lower_overlap: float
    box_overlap: float
    foot_in: bool
    foot_unreliable: str  # "", "person_occlusion", or "frame_edge"

    def to_json(self) -> dict:
        data = asdict(self)
        for key in ("confidence", "support", "bottom_support", "lower_overlap", "box_overlap"):
            data[key] = round(data[key], 3)
        data["box"] = [round(v, 4) for v in self.box]
        return data


@dataclass(frozen=True)
class FrameEvidence:
    evidence: Evidence
    visible_fraction: float
    dogs: tuple[DogEvidence, ...]
    person_count: int

    def to_json(self) -> dict:
        return {
            "evidence": self.evidence.value,
            "visible_fraction": round(self.visible_fraction, 3),
            "person_count": self.person_count,
            "dogs": [d.to_json() for d in self.dogs],
        }


def _intersection_fraction(band, other) -> float:
    """Share of `band` covered by `other`."""
    x1, y1 = max(band[0], other[0]), max(band[1], other[1])
    x2, y2 = min(band[2], other[2]), min(band[3], other[3])
    area = (band[2] - band[0]) * (band[3] - band[1])
    if x2 <= x1 or y2 <= y1 or area <= 0:
        return 0.0
    return (x2 - x1) * (y2 - y1) / area


def score_dog(det: Detection, mask: CushionMask, people: list[tuple], cfg: ClassifierConfig,
              dog_confidence: float) -> DogEvidence:
    x1, y1, x2, y2 = det.box
    width, height = x2 - x1, y2 - y1
    inset = width * (1 - FOOT_WIDTH) / 2

    box_overlap = mask.fraction(x1, y1, x2, y2)
    lower_band = (x1, y2 - height * LOWER_BAND, x2, y2)
    lower_overlap = mask.fraction(*lower_band)
    bottom_support = mask.fraction(x1 + inset, y2 - height * FOOT_BAND, x2 - inset, y2)
    foot_in = mask.contains((x1 + x2) / 2, y2)
    support = W_BOTTOM * bottom_support + W_LOWER * lower_overlap + W_BOX * box_overlap

    unreliable = ""
    if y2 >= 1.0 - FRAME_EDGE:
        unreliable = "frame_edge"
    elif any(_intersection_fraction(lower_band, p) >= cfg.occlusion_fraction for p in people):
        unreliable = "person_occlusion"

    confident = det.confidence >= dog_confidence
    if support >= cfg.on_support:
        label = LABEL_ON if confident else LABEL_AMBIGUOUS
    elif support >= cfg.ambiguous_support or box_overlap >= cfg.ambiguous_box_overlap:
        label = LABEL_AMBIGUOUS
    elif unreliable and mask.fraction(x1 - width * NEAR_MARGIN, y1 - height * NEAR_MARGIN,
                                      x2 + width * NEAR_MARGIN, y2 + height * NEAR_MARGIN) > 0:
        # Its bottom edge is hidden and it is beside the cushions, so it may be
        # sitting on them behind someone.
        label = LABEL_AMBIGUOUS
    elif confident:
        label = LABEL_OFF
    else:
        label = LABEL_WEAK

    return DogEvidence(det.box, det.confidence, label, support, bottom_support,
                       lower_overlap, box_overlap, foot_in, unreliable)


def classify_frame(dogs: list[Detection], persons: list[Detection], mask: CushionMask,
                   cfg: ClassifierConfig, dog_confidence: float) -> FrameEvidence:
    """Aggregate over all dogs. Identity is not modelled: any dog on a cushion is ON."""
    person_boxes = [p.box for p in persons]
    scored = tuple(score_dog(d, mask, person_boxes, cfg, dog_confidence) for d in dogs)
    labels = {d.label for d in scored}
    visible = 1.0 - mask.covered_fraction(person_boxes)

    if LABEL_ON in labels:
        evidence = Evidence.ON
    elif LABEL_AMBIGUOUS in labels:
        evidence = Evidence.AMBIGUOUS
    elif LABEL_OFF in labels:
        evidence = Evidence.CLEAR_SEEN
    elif visible < cfg.min_visible_fraction:
        evidence = Evidence.OBSTRUCTED
    else:
        evidence = Evidence.CLEAR_EMPTY
    return FrameEvidence(evidence, visible, scored, len(persons))
