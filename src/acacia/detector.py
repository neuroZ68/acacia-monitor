"""Detector boundary: frame in, detections, timing, and errors out.

The pipeline depends only on `Detector.detect`. Swapping the model or runtime
means writing one new class, with no change to geometry, state, or the UI.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image

COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)

RELEASE_URL = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    filename: str
    sha256: str
    size_bytes: int
    input_size: int

    @property
    def url(self) -> str:
        return f"{RELEASE_URL}/{self.filename}"


MODELS = {
    "yolox_tiny": ModelSpec(
        "yolox_tiny", "yolox_tiny.onnx",
        "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7", 20219662, 416),
    "yolox_s": ModelSpec(
        "yolox_s", "yolox_s.onnx",
        "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063", 35858002, 640),
    "yolox_m": ModelSpec(
        "yolox_m", "yolox_m.onnx",
        "21ff6cfdeb53b013bac2249599e55f00bff3cfdfdab37ed7a4620818c1d15b3f", 101259744, 640),
}


class DetectorError(Exception):
    pass


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    box: tuple[float, float, float, float]  # normalized x1, y1, x2, y2


@dataclass(frozen=True)
class DetectionResult:
    detections: tuple[Detection, ...]
    preprocess_ms: float
    inference_ms: float
    postprocess_ms: float

    def of(self, label: str) -> list[Detection]:
        return [d for d in self.detections if d.label == label]


class Detector(Protocol):
    name: str
    provider: str

    def detect(self, rgb: np.ndarray, thresholds: dict[str, float]) -> DetectionResult:
        """`thresholds` maps a class label to its minimum confidence."""
        ...

    def close(self) -> None:
        ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def letterbox(rgb: np.ndarray, size: int) -> tuple[np.ndarray, float]:
    """Resize preserving aspect into a size-by-size canvas padded with 114.

    Returns a float32 NCHW tensor in BGR order on a 0-255 scale, which is what
    the official YOLOX ONNX exports expect, plus the resize ratio.
    """
    height, width = rgb.shape[:2]
    ratio = min(size / height, size / width)
    new_w, new_h = int(width * ratio), int(height * ratio)
    resized = np.asarray(Image.fromarray(rgb).resize((new_w, new_h), Image.BILINEAR))
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[:new_h, :new_w] = resized[:, :, ::-1]
    return np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32), ratio


def yolox_grids(size: int) -> tuple[np.ndarray, np.ndarray]:
    grids, strides = [], []
    for stride in (8, 16, 32):
        cells = size // stride
        xs, ys = np.meshgrid(np.arange(cells), np.arange(cells))
        grids.append(np.stack((xs, ys), 2).reshape(-1, 2))
        strides.append(np.full((cells * cells, 1), stride))
    return np.concatenate(grids, 0).astype(np.float32), np.concatenate(strides, 0).astype(np.float32)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    order = scores.argsort()[::-1]
    area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    keep: list[int] = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        rest = order[1:]
        x1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        y1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        x2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        y2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
        iou = inter / (area[i] + area[rest] - inter + 1e-9)
        order = rest[iou <= iou_threshold]
    return keep


class YoloxOnnxDetector:
    def __init__(self, spec: ModelSpec, models_dir: Path, provider: str = "auto", nms_iou: float = 0.45):
        import onnxruntime as ort

        path = models_dir / spec.filename
        if not path.exists():
            raise DetectorError(f"Model {spec.filename} missing. Run: ./acacia model fetch")
        if sha256_file(path) != spec.sha256:
            raise DetectorError(f"Model {spec.filename} failed its SHA-256 check")

        # Providers are named explicitly: the wheel also lists an Azure provider
        # that must never be selected.
        available = ort.get_available_providers()
        if provider == "coreml":
            wanted = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        elif provider == "cpu":
            wanted = ["CPUExecutionProvider"]
        else:
            wanted = [p for p in ("CoreMLExecutionProvider", "CPUExecutionProvider") if p in available]
        if provider == "coreml" and "CoreMLExecutionProvider" not in available:
            raise DetectorError("CoreML provider is not available on this machine")

        options = ort.SessionOptions()
        options.log_severity_level = 3
        self.session = ort.InferenceSession(str(path), sess_options=options, providers=wanted)
        self.input_name = self.session.get_inputs()[0].name
        self.size = spec.input_size
        self.nms_iou = nms_iou
        self.name = spec.name
        self.provider = self.session.get_providers()[0].replace("ExecutionProvider", "")
        self.grids, self.strides = yolox_grids(self.size)

    def detect(self, rgb: np.ndarray, thresholds: dict[str, float]) -> DetectionResult:
        t0 = time.perf_counter()
        tensor, ratio = letterbox(rgb, self.size)
        t1 = time.perf_counter()
        raw = self.session.run(None, {self.input_name: tensor})[0][0]
        t2 = time.perf_counter()
        detections = self._decode(raw, ratio, rgb.shape[1], rgb.shape[0], thresholds)
        t3 = time.perf_counter()
        return DetectionResult(tuple(detections), (t1 - t0) * 1e3, (t2 - t1) * 1e3, (t3 - t2) * 1e3)

    def _decode(self, raw: np.ndarray, ratio: float, width: int, height: int,
                thresholds: dict[str, float]) -> list[Detection]:
        out: list[Detection] = []
        objectness = raw[:, 4]
        for label, threshold in thresholds.items():
            scores = objectness * raw[:, 5 + COCO_CLASSES.index(label)]
            rows = np.nonzero(scores >= threshold)[0]
            if not rows.size:
                continue
            centers = (raw[rows, :2] + self.grids[rows]) * self.strides[rows]
            sizes = np.exp(raw[rows, 2:4]) * self.strides[rows]
            boxes = np.concatenate((centers - sizes / 2, centers + sizes / 2), axis=1) / ratio
            for i in nms(boxes, scores[rows], self.nms_iou):
                x1, y1, x2, y2 = boxes[i]
                out.append(Detection(label, float(scores[rows][i]), (
                    float(np.clip(x1 / width, 0, 1)), float(np.clip(y1 / height, 0, 1)),
                    float(np.clip(x2 / width, 0, 1)), float(np.clip(y2 / height, 0, 1)))))
        return out

    def close(self) -> None:
        self.session = None
