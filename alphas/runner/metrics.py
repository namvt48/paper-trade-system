from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunnerMetrics:
    warmup_cache_hits_total: int = 0
    warmup_snapshot_hits_total: int = 0
    warmup_mds_requests_total: int = 0
    warmup_mds_symbols_requested_total: int = 0
    warmup_timeouts_total: int = 0
    warmup_partial_ready_total: int = 0
    warmup_request_duration_sec: list[float] = field(default_factory=list)
    strategy_readiness_coverage: dict[str, float] = field(default_factory=dict)

    def inc(self, name: str, amount: int = 1) -> None:
        setattr(self, name, int(getattr(self, name)) + int(amount))

    def observe_duration(self, seconds: float) -> None:
        self.warmup_request_duration_sec.append(float(seconds))

    def set_strategy_coverage(self, alpha_id: str, coverage: float) -> None:
        self.strategy_readiness_coverage[str(alpha_id)] = float(coverage)

    def snapshot(self) -> dict[str, Any]:
        return {
            "warmup_cache_hits_total": self.warmup_cache_hits_total,
            "warmup_snapshot_hits_total": self.warmup_snapshot_hits_total,
            "warmup_mds_requests_total": self.warmup_mds_requests_total,
            "warmup_mds_symbols_requested_total": self.warmup_mds_symbols_requested_total,
            "warmup_timeouts_total": self.warmup_timeouts_total,
            "warmup_partial_ready_total": self.warmup_partial_ready_total,
            "warmup_request_duration_sec": list(self.warmup_request_duration_sec),
            "strategy_readiness_coverage": dict(self.strategy_readiness_coverage),
        }
