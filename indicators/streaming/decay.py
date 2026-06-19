from __future__ import annotations

from collections import deque


class DecayLinear:
    """Linearly-weighted MA, newest weight d..oldest 1. O(1): WS = WS - S + d*x."""
    __slots__ = ("d", "buf", "S", "WS", "norm")

    def __init__(self, d: int):
        self.d = d
        self.buf: deque[float] = deque()
        self.S = 0.0
        self.WS = 0.0
        self.norm = d * (d + 1) / 2.0

    def update(self, x: float) -> DecayLinear:
        if x != x:
            return self
        d, b = self.d, self.buf
        if len(b) == d:
            old = b[0]
            self.WS = self.WS - self.S + d * x
            self.S = self.S - old + x
            b.append(x)
            b.popleft()
        else:
            b.append(x)
            self.S += x
            if len(b) == d:
                self.WS = sum((j + 1) * b[j] for j in range(d))
        return self

    def value(self) -> float:
        return self.WS / self.norm if len(self.buf) == self.d else float("nan")
