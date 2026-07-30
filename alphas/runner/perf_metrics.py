"""Bounded latency summaries for runner observability.

The module owns in-memory aggregation only. It intentionally retains a fixed
number of recent samples so the long-running metrics endpoint cannot grow with
event volume or dynamic universe cardinality.
"""

from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import TypedDict


@dataclass(frozen=True, slots=True)
class InvalidMetricCapacity(ValueError):
    """Name the invalid bounded-metric dimension and rejected capacity."""

    field: str
    value: int

    def __str__(self) -> str:
        return f"{self.field} must be positive, got {self.value}"


class LatencySnapshot(TypedDict):
    """Serializable latency statistics for one bounded observation window."""

    count_total: int
    sample_count: int
    avg: float
    max: float
    p50: float
    p95: float
    p99: float


class LatencyWindow:
    """Accumulate total count while retaining only the newest latency samples."""

    def __init__(self, max_samples: int = 2048) -> None:
        if max_samples < 1:
            raise InvalidMetricCapacity(field="max_samples", value=max_samples)
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._count_total: int = 0

    def observe(self, value: float) -> None:
        """Record one non-negative duration or queue-depth observation."""
        self._samples.append(max(0.0, float(value)))
        self._count_total += 1

    def snapshot(self) -> LatencySnapshot:
        """Return nearest-rank quantiles over the retained recent samples."""
        if not self._samples:
            return {
                "count_total": self._count_total,
                "sample_count": 0,
                "avg": 0.0,
                "max": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
            }
        ordered = sorted(self._samples)
        return {
            "count_total": self._count_total,
            "sample_count": len(ordered),
            "avg": sum(ordered) / len(ordered),
            "max": ordered[-1],
            "p50": self._quantile(ordered, 0.50),
            "p95": self._quantile(ordered, 0.95),
            "p99": self._quantile(ordered, 0.99),
        }

    @staticmethod
    def _quantile(ordered: list[float], quantile: float) -> float:
        rank = max(0, math.ceil(quantile * len(ordered)) - 1)
        return ordered[rank]


class LabeledLatencyWindows:
    """Keep an LRU-bounded set of per-label latency windows."""

    def __init__(self, max_labels: int = 32, max_samples: int = 256) -> None:
        if max_labels < 1:
            raise InvalidMetricCapacity(field="max_labels", value=max_labels)
        self._max_labels: int = max_labels
        self._max_samples: int = max_samples
        self._windows: OrderedDict[str, LatencyWindow] = OrderedDict()

    def observe(self, label: str, value: float) -> None:
        """Record a sample and evict the least-recently-used label if needed."""
        window = self._windows.get(label)
        if window is None:
            window = LatencyWindow(self._max_samples)
            self._windows[label] = window
            while len(self._windows) > self._max_labels:
                _ = self._windows.popitem(last=False)
        else:
            self._windows.move_to_end(label)
        window.observe(value)

    def snapshot(self) -> dict[str, LatencySnapshot]:
        """Return summaries in least-to-most-recently-used label order."""
        return {label: window.snapshot() for label, window in self._windows.items()}
