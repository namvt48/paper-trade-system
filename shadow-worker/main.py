from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import redis


def logical_signal_key(signal: dict[str, Any]) -> str:
    signal_type = signal.get("signal_type") or signal.get("type") or ""
    candle_open = signal.get("signal_candle_open_ms") or signal.get("candle_open_ms") or signal.get("open_time") or ""
    version = signal.get("strategy_version") or signal.get("version") or ""
    return "|".join([
        str(signal.get("alpha_id", "")),
        str(signal.get("symbol", "")),
        str(signal.get("side", "")),
        str(signal_type),
        str(candle_open),
        str(version),
    ])


def signal_emit_ms(signal: dict[str, Any]) -> int:
    for key in ("emitted_at_ms", "ts_ms", "timestamp_ms"):
        try:
            value = int(signal.get(key, 0))
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return int(time.time() * 1000)


@dataclass
class ShadowStats:
    matched: int = 0
    production_only: int = 0
    shadow_only: int = 0
    latency_deltas_ms: list[int] = field(default_factory=list)
    sample_mismatches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        total = self.matched + self.production_only + self.shadow_only
        return self.matched / total if total else 1.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "production_only": self.production_only,
            "shadow_only": self.shadow_only,
            "match_rate": self.match_rate,
            "latency_deltas_ms": list(self.latency_deltas_ms[-100:]),
            "sample_mismatches": list(self.sample_mismatches[-20:]),
        }


class SignalComparator:
    def __init__(self, ttl_sec: float = 300.0, now_func=None) -> None:
        self.ttl_sec = float(ttl_sec)
        self._now = now_func or time.time
        self._production: dict[str, tuple[float, dict[str, Any]]] = {}
        self._shadow: dict[str, tuple[float, dict[str, Any]]] = {}
        self.stats = ShadowStats()

    def observe(self, source: str, signal: dict[str, Any]) -> bool:
        key = logical_signal_key(signal)
        if source == "production":
            other = self._shadow.pop(key, None)
            if other is not None:
                self._match(signal, other[1])
                return True
            self._production[key] = (self._now(), signal)
            return False
        if source == "shadow":
            other = self._production.pop(key, None)
            if other is not None:
                self._match(other[1], signal)
                return True
            self._shadow[key] = (self._now(), signal)
            return False
        raise ValueError(f"unknown source: {source}")

    def expire(self) -> None:
        now = self._now()
        for key, (seen_at, signal) in list(self._production.items()):
            if now - seen_at >= self.ttl_sec:
                self._production.pop(key, None)
                self.stats.production_only += 1
                self._sample("production_only", key, signal)
        for key, (seen_at, signal) in list(self._shadow.items()):
            if now - seen_at >= self.ttl_sec:
                self._shadow.pop(key, None)
                self.stats.shadow_only += 1
                self._sample("shadow_only", key, signal)

    def _match(self, production: dict[str, Any], shadow: dict[str, Any]) -> None:
        self.stats.matched += 1
        self.stats.latency_deltas_ms.append(abs(signal_emit_ms(shadow) - signal_emit_ms(production)))

    def _sample(self, kind: str, key: str, signal: dict[str, Any]) -> None:
        self.stats.sample_mismatches.append({"kind": kind, "key": key, "signal": signal})


def decode_fields(fields: dict[str, Any]) -> dict[str, Any]:
    decoded = {}
    for key, value in fields.items():
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        decoded[str(key)] = value
    return decoded


def run_worker() -> None:
    redis_url = os.getenv("REDIS_URL", "redis://paper-redis:6379")
    production_stream = os.getenv("PRODUCTION_STREAM", "paper-signals")
    shadow_stream = os.getenv("SHADOW_STREAM", "paper-signals-shadow")
    ttl_sec = float(os.getenv("SHADOW_TTL_SEC", "300"))
    poll_ms = int(os.getenv("SHADOW_POLL_MS", "1000"))

    client = redis.from_url(redis_url, decode_responses=True)
    comparator = SignalComparator(ttl_sec=ttl_sec)
    last_ids = {production_stream: "$", shadow_stream: "$"}

    while True:
        messages = client.xread(last_ids, count=100, block=poll_ms)
        for stream, entries in messages or []:
            source = "production" if stream == production_stream else "shadow"
            for msg_id, fields in entries:
                last_ids[stream] = msg_id
                comparator.observe(source, decode_fields(fields))
        comparator.expire()
        print(json.dumps(comparator.stats.snapshot(), sort_keys=True), flush=True)


if __name__ == "__main__":
    run_worker()
