from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


BOOK_KEY_PREFIX = "book:target:"
BOOK_CHANNEL_PREFIX = "book:updated:"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TargetBook:
    """Latest-wins, normalized target book published by one sleeve."""

    sleeve_id: str
    revision: int
    generated_at: str
    as_of_candle_ms: int
    timeframe: str
    gross: float
    net: float
    weights: Mapping[str, float]
    schema_version: int = 1
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sleeve_id:
            raise ValueError("sleeve_id is required")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if self.as_of_candle_ms < 0:
            raise ValueError("as_of_candle_ms must be non-negative")
        if not self.timeframe:
            raise ValueError("timeframe is required")
        for symbol, weight in self.weights.items():
            if not symbol or not math.isfinite(float(weight)):
                raise ValueError(f"invalid weight for {symbol!r}")
        if not math.isfinite(float(self.gross)) or self.gross < 0:
            raise ValueError("gross must be finite and non-negative")
        if not math.isfinite(float(self.net)):
            raise ValueError("net must be finite")

    @classmethod
    def create(
        cls,
        sleeve_id: str,
        timeframe: str,
        weights: Mapping[str, float],
        *,
        revision: int,
        as_of_candle_ms: int,
        generated_at: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> "TargetBook":
        clean = {str(symbol): float(weight) for symbol, weight in weights.items() if float(weight) != 0.0}
        return cls(
            sleeve_id=sleeve_id,
            revision=revision,
            generated_at=generated_at or _utc_now(),
            as_of_candle_ms=as_of_candle_ms,
            timeframe=timeframe,
            gross=sum(abs(weight) for weight in clean.values()),
            net=sum(clean.values()),
            weights=clean,
            meta=dict(meta or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sleeve_id": self.sleeve_id,
            "revision": self.revision,
            "generated_at": self.generated_at,
            "as_of_candle_ms": self.as_of_candle_ms,
            "timeframe": self.timeframe,
            "gross": self.gross,
            "net": self.net,
            "weights": dict(self.weights),
            "meta": dict(self.meta),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TargetBook":
        required = ("sleeve_id", "revision", "generated_at", "as_of_candle_ms", "timeframe", "weights")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"target book missing fields: {', '.join(missing)}")
        weights = {str(symbol): float(value) for symbol, value in dict(raw["weights"]).items()}
        gross = float(raw.get("gross", sum(abs(value) for value in weights.values())))
        net = float(raw.get("net", sum(weights.values())))
        return cls(
            schema_version=int(raw.get("schema_version", 1)),
            sleeve_id=str(raw["sleeve_id"]),
            revision=int(raw["revision"]),
            generated_at=str(raw["generated_at"]),
            as_of_candle_ms=int(raw["as_of_candle_ms"]),
            timeframe=str(raw["timeframe"]),
            gross=gross,
            net=net,
            weights=weights,
            meta=dict(raw.get("meta") or {}),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "TargetBook":
        return cls.from_dict(json.loads(raw))

    def age_seconds(self, now: float | None = None) -> float:
        timestamp = datetime.fromisoformat(self.generated_at.replace("Z", "+00:00")).timestamp()
        return max(0.0, (time.time() if now is None else now) - timestamp)

    def is_stale(self, max_staleness_sec: float, now: float | None = None) -> bool:
        return self.age_seconds(now) > float(max_staleness_sec)


class TargetBookStore:
    """Small Redis adapter; Redis is intentionally duck-typed for unit tests."""

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str = BOOK_KEY_PREFIX,
        channel_prefix: str = BOOK_CHANNEL_PREFIX,
    ) -> None:
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.channel_prefix = channel_prefix

    def key(self, sleeve_id: str) -> str:
        return f"{self.key_prefix}{sleeve_id}"

    def channel(self, sleeve_id: str) -> str:
        return f"{self.channel_prefix}{sleeve_id}"

    def write(self, book: TargetBook) -> None:
        self.redis.set(self.key(book.sleeve_id), book.to_json())
        self.redis.publish(self.channel(book.sleeve_id), str(book.revision))

    def read(self, sleeve_id: str) -> TargetBook | None:
        raw = self.redis.get(self.key(sleeve_id))
        if raw is None:
            return None
        return TargetBook.from_json(raw)
