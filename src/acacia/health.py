"""Rolling timing and resource metrics. Bounded in memory by construction."""

from __future__ import annotations

import os
import threading
import time
from collections import deque

import psutil


class Rolling:
    """Recent samples of one timing, for median, p95, and max."""

    def __init__(self, maxlen: int = 512):
        self._values: deque[float] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, value: float) -> None:
        with self._lock:
            self._values.append(value)

    def stats(self) -> dict | None:
        with self._lock:
            values = sorted(self._values)
        if not values:
            return None
        count = len(values)
        result = {"n": count, "median": round(values[count // 2], 1), "max": round(values[-1], 1)}
        # An upper percentile from a handful of samples would be false precision.
        if count >= 20:
            result["p95"] = round(values[min(count - 1, int(count * 0.95))], 1)
        return result


class RateMeter:
    """Events per second over a sliding window."""

    def __init__(self, window_s: float = 10.0):
        self.window_s = window_s
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def tick(self, t: float | None = None) -> None:
        t = time.monotonic() if t is None else t
        with self._lock:
            self._times.append(t)
            self._trim(t)

    def _trim(self, t: float) -> None:
        while self._times and t - self._times[0] > self.window_s:
            self._times.popleft()

    def rate(self) -> float:
        t = time.monotonic()
        with self._lock:
            self._trim(t)
            if len(self._times) < 2:
                return 0.0
            span = max(t - self._times[0], 1e-3)
            return round(len(self._times) / span, 2)


class Metrics:
    def __init__(self) -> None:
        self.decoded = RateMeter()
        self.inferred = RateMeter()
        self.decisions = RateMeter()
        self.ui_frames = RateMeter()
        self.frame_age_at_inference_ms = Rolling()
        self.inference_ms = Rolling()
        self.preprocess_ms = Rolling()
        self.decode_to_decision_ms = Rolling()
        self.decision_to_ui_ack_ms = Rolling()
        self.frame_to_ui_ack_ms = Rolling()
        self.dropped_stale = 0
        self.inference_failures = 0
        self._process = psutil.Process(os.getpid())
        self._process.cpu_percent(None)  # first call primes the counter

    def timing_json(self) -> dict:
        return {
            "decoded_fps": self.decoded.rate(),
            "inference_fps": self.inferred.rate(),
            "decision_fps": self.decisions.rate(),
            "ui_fps": self.ui_frames.rate(),
            "frame_age_at_inference_ms": self.frame_age_at_inference_ms.stats(),
            "preprocess_ms": self.preprocess_ms.stats(),
            "inference_ms": self.inference_ms.stats(),
            "decode_to_decision_ms": self.decode_to_decision_ms.stats(),
            "decision_to_ui_ack_ms": self.decision_to_ui_ack_ms.stats(),
            "frame_to_ui_ack_ms": self.frame_to_ui_ack_ms.stats(),
            "dropped_stale": self.dropped_stale,
            "inference_failures": self.inference_failures,
        }

    def resources_json(self) -> dict:
        with self._process.oneshot():
            memory = self._process.memory_info().rss
            threads = self._process.num_threads()
        return {
            # psutil reports percent of one core, so 200 means two full cores.
            "process_cpu_percent_of_one_core": self._process.cpu_percent(None),
            "rss_mb": round(memory / 1e6, 1),
            "threads": threads,
        }
