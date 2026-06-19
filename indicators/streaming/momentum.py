from __future__ import annotations

from collections import deque


class Momentum:
    """ts_momentum(x, d) = x_t / x_{t-d} - 1."""
    __slots__ = ("d", "buf")

    def __init__(self, d: int):
        self.d = d
        self.buf: deque[float] = deque(maxlen=d + 1)

    def update(self, x: float) -> Momentum:
        if x == x:
            self.buf.append(x)
        return self

    def value(self) -> float:
        if len(self.buf) <= self.d:
            return float("nan")
        o = self.buf[0]
        return self.buf[-1] / o - 1.0 if o else float("nan")
