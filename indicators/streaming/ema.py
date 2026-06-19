from __future__ import annotations


class EMA:
    __slots__ = ("a", "y", "n", "minp")

    def __init__(self, span: int):
        self.a = 2.0 / (span + 1.0)
        self.y: float | None = None
        self.n = 0
        self.minp = max(1, span // 2)

    def update(self, x: float) -> EMA:
        if x == x:
            self.y = x if self.y is None else self.a * x + (1 - self.a) * self.y
            self.n += 1
        return self

    def value(self) -> float:
        return self.y if self.n >= self.minp else float("nan")
