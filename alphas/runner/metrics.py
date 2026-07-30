from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runner.perf_metrics import LatencyWindow


@dataclass(slots=True)  # noqa: MUTABLE_OK
class RunnerMetrics:
    """Accumulate process-local counters; mutation is the class's sole purpose."""

    warmup_cache_hits_total: int = 0
    warmup_snapshot_hits_total: int = 0
    warmup_mds_requests_total: int = 0
    warmup_mds_symbols_requested_total: int = 0
    warmup_timeouts_total: int = 0
    warmup_partial_ready_total: int = 0
    warmup_request_duration_sec: list[float] = field(default_factory=list)
    strategy_readiness_coverage: dict[str, float] = field(default_factory=dict)
    stale_check_total: int = 0
    stale_detected_total: int = 0
    reconnect_warmup_total: int = 0
    reconnect_snapshot_hit_total: int = 0
    reconnect_full_warmup_total: int = 0
    pubsub_connection_error_total: int = 0
    scan_timeout_by_alpha: dict[str, int] = field(default_factory=dict)
    last_event_ts_by_alpha: dict[str, float] = field(default_factory=dict)
    signals_dispatched_total: int = 0
    signals_dedup_skipped_total: int = 0
    signals_lease_dropped_total: int = 0
    signals_xadd_published_total: int = 0
    signals_dedup_skipped_by_alpha: dict[str, int] = field(default_factory=dict)
    signals_lease_dropped_by_alpha: dict[str, int] = field(default_factory=dict)
    event_total: int = 0
    scan_total: int = 0
    event_by_kind: dict[str, int] = field(default_factory=dict)
    scan_waiters_current: int = 0
    scan_waiters_max: int = 0
    queue_wait_ms: LatencyWindow = field(default_factory=LatencyWindow)
    semaphore_wait_ms: LatencyWindow = field(default_factory=LatencyWindow)
    scan_ms: LatencyWindow = field(default_factory=LatencyWindow)
    event_total_ms: LatencyWindow = field(default_factory=LatencyWindow)

    def inc(self, name: str, amount: int = 1) -> None:
        setattr(self, name, int(getattr(self, name)) + int(amount))

    def observe_duration(self, seconds: float) -> None:
        self.warmup_request_duration_sec.append(float(seconds))

    def set_strategy_coverage(self, alpha_id: str, coverage: float) -> None:
        self.strategy_readiness_coverage[str(alpha_id)] = float(coverage)

    def inc_scan_timeout(self, alpha_id: str) -> None:
        self.scan_timeout_by_alpha[alpha_id] = self.scan_timeout_by_alpha.get(alpha_id, 0) + 1

    def inc_signal_dispatched(self) -> None:
        self.signals_dispatched_total += 1

    def inc_signal_published(self) -> None:
        self.signals_xadd_published_total += 1

    def inc_signal_dedup_skipped(self, alpha_id: str) -> None:
        self.signals_dedup_skipped_total += 1
        self.signals_dedup_skipped_by_alpha[alpha_id] = (
            self.signals_dedup_skipped_by_alpha.get(alpha_id, 0) + 1
        )

    def inc_signal_lease_dropped(self, alpha_id: str) -> None:
        self.signals_lease_dropped_total += 1
        self.signals_lease_dropped_by_alpha[alpha_id] = (
            self.signals_lease_dropped_by_alpha.get(alpha_id, 0) + 1
        )

    def mark_event_processed(self, alpha_id: str, now: float) -> None:
        self.last_event_ts_by_alpha[alpha_id] = float(now)

    def scan_wait_started(self) -> None:
        """Track one event waiting for admission to the shared scan pool."""
        self.scan_waiters_current += 1
        self.scan_waiters_max = max(self.scan_waiters_max, self.scan_waiters_current)

    def scan_wait_finished(self) -> None:
        """Remove one event from the admission-wait gauge."""
        self.scan_waiters_current = max(0, self.scan_waiters_current - 1)

    def observe_event(
        self,
        *,
        kind: str,
        queue_wait_ms: float,
        semaphore_wait_ms: float,
        scan_ms: float,
        total_ms: float,
        scanned: bool,
    ) -> None:
        """Record the latency breakdown for one successfully processed event."""
        self.event_total += 1
        self.event_by_kind[kind] = self.event_by_kind.get(kind, 0) + 1
        if scanned:
            self.scan_total += 1
            self.scan_ms.observe(scan_ms)
        self.queue_wait_ms.observe(queue_wait_ms)
        self.semaphore_wait_ms.observe(semaphore_wait_ms)
        self.event_total_ms.observe(total_ms)

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
            "stale_check_total": self.stale_check_total,
            "stale_detected_total": self.stale_detected_total,
            "reconnect_warmup_total": self.reconnect_warmup_total,
            "reconnect_snapshot_hit_total": self.reconnect_snapshot_hit_total,
            "reconnect_full_warmup_total": self.reconnect_full_warmup_total,
            "pubsub_connection_error_total": self.pubsub_connection_error_total,
            "scan_timeout_by_alpha": dict(self.scan_timeout_by_alpha),
            "last_event_ts_by_alpha": dict(self.last_event_ts_by_alpha),
            "signals_dispatched_total": self.signals_dispatched_total,
            "signals_dedup_skipped_total": self.signals_dedup_skipped_total,
            "signals_lease_dropped_total": self.signals_lease_dropped_total,
            "signals_xadd_published_total": self.signals_xadd_published_total,
            "signals_dedup_skipped_by_alpha": dict(self.signals_dedup_skipped_by_alpha),
            "signals_lease_dropped_by_alpha": dict(self.signals_lease_dropped_by_alpha),
            "performance": {
                "event_total": self.event_total,
                "scan_total": self.scan_total,
                "event_by_kind": dict(self.event_by_kind),
                "scan_waiters_current": self.scan_waiters_current,
                "scan_waiters_max": self.scan_waiters_max,
                "queue_wait_ms": self.queue_wait_ms.snapshot(),
                "semaphore_wait_ms": self.semaphore_wait_ms.snapshot(),
                "scan_ms": self.scan_ms.snapshot(),
                "total_ms": self.event_total_ms.snapshot(),
            },
        }
