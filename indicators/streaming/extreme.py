from __future__ import annotations

from collections import deque


class RollingExtreme:
    """ts_min / ts_max in O(1) amortised via a monotonic deque."""
    __slots__ = ("d", "dq", "t", "is_max")

    def __init__(self, d: int, is_max: bool = True):
        self.d = d
        self.dq: deque[tuple[int, float]] = deque()
        self.t = 0
        self.is_max = is_max

    def update(self, x: float) -> RollingExtreme:
        if x != x:
            return self
        dq = self.dq
        while dq and ((dq[-1][1] <= x) if self.is_max else (dq[-1][1] >= x)):
            dq.pop()
        dq.append((self.t, x))
        while dq[0][0] <= self.t - self.d:
            dq.popleft()
        self.t += 1
        return self

    def value(self) -> float:
        return self.dq[0][1] if self.t >= self.d else float("nan")
