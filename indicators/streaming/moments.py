from __future__ import annotations

from collections import deque


class RollingMoments:
    """Window mean / std(ddof=1) / zscore / skew via running power sums."""
    __slots__ = ("d", "buf", "s1", "s2", "s3")

    def __init__(self, d: int):
        self.d = d
        self.buf: deque[float] = deque()
        self.s1 = self.s2 = self.s3 = 0.0

    def update(self, x: float) -> RollingMoments:
        if x != x:
            return self
        b = self.buf
        b.append(x)
        self.s1 += x
        self.s2 += x * x
        self.s3 += x * x * x
        if len(b) > self.d:
            o = b.popleft()
            self.s1 -= o
            self.s2 -= o * o
            self.s3 -= o * o * o
        return self

    def mean(self) -> float:
        n = len(self.buf)
        return self.s1 / n if n else float("nan")

    def std(self) -> float:
        n = len(self.buf)
        if n < 2:
            return float("nan")
        m = self.s1 / n
        v = (self.s2 - n * m * m) / (n - 1)
        return v ** 0.5 if v > 0 else float("nan")

    def zscore(self, x: float) -> float:
        s = self.std()
        return (x - self.mean()) / s if s == s and s > 0 else float("nan")

    def skew(self) -> float:
        n = len(self.buf)
        if n < 3:
            return float("nan")
        m = self.s1 / n
        m2 = self.s2 / n - m * m
        m3 = self.s3 / n - 3 * m * (self.s2 / n) + 2 * m * m * m
        return m3 / m2 ** 1.5 if m2 > 1e-12 else float("nan")
