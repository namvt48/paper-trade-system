from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkerMetrics:
    """Process-local consumer counters for signal reconciliation ("no silent failures").

    Invariant (per process lifetime):
        received_total == duplicate_skipped_total + parse_error_total
                          + process_error_total + committed_total
                          + left_pending_total
    where committed_total == sum(committed_by_type.values()).

    ``left_pending_total`` counts messages an unexpected exception (e.g. a
    SQLite "database is locked" past busy_timeout) prevented from reaching any
    of the other buckets; the message is left un-acked in the stream's PEL for
    manual XCLAIM-based review rather than acked-and-lost. It should stay at
    zero in steady state -- any non-zero value is itself the alert.

    These reset on restart; the durable source of truth for audit is SQLite +
    the Redis stream (see scripts/reconcile_signals.py). Counters here are for
    live observation only.
    """

    received_total: int = 0
    duplicate_skipped_total: int = 0
    parse_error_total: int = 0
    process_error_total: int = 0
    left_pending_total: int = 0
    xack_total: int = 0
    committed_by_type: dict[str, int] = field(default_factory=dict)
    parse_error_by_alpha: dict[str, int] = field(default_factory=dict)
    process_error_by_alpha: dict[str, int] = field(default_factory=dict)
    left_pending_by_alpha: dict[str, int] = field(default_factory=dict)

    def inc(self, name: str, amount: int = 1) -> None:
        setattr(self, name, int(getattr(self, name)) + int(amount))

    def inc_committed(self, signal_type: str) -> None:
        key = str(signal_type)
        self.committed_by_type[key] = self.committed_by_type.get(key, 0) + 1

    def inc_parse_error(self, alpha_id: str) -> None:
        self.parse_error_total += 1
        self.parse_error_by_alpha[alpha_id] = (
            self.parse_error_by_alpha.get(alpha_id, 0) + 1
        )

    def inc_process_error(self, alpha_id: str) -> None:
        self.process_error_total += 1
        self.process_error_by_alpha[alpha_id] = (
            self.process_error_by_alpha.get(alpha_id, 0) + 1
        )

    def inc_left_pending(self, alpha_id: str) -> None:
        self.left_pending_total += 1
        self.left_pending_by_alpha[alpha_id] = (
            self.left_pending_by_alpha.get(alpha_id, 0) + 1
        )

    @property
    def committed_total(self) -> int:
        return sum(self.committed_by_type.values())

    def reconciles(self) -> bool:
        """True when the internal received-vs-accounted invariant holds."""
        return self.received_total == (
            self.duplicate_skipped_total
            + self.parse_error_total
            + self.process_error_total
            + self.committed_total
            + self.left_pending_total
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "received_total": self.received_total,
            "duplicate_skipped_total": self.duplicate_skipped_total,
            "parse_error_total": self.parse_error_total,
            "process_error_total": self.process_error_total,
            "left_pending_total": self.left_pending_total,
            "committed_total": self.committed_total,
            "xack_total": self.xack_total,
            "committed_by_type": dict(self.committed_by_type),
            "parse_error_by_alpha": dict(self.parse_error_by_alpha),
            "process_error_by_alpha": dict(self.process_error_by_alpha),
            "left_pending_by_alpha": dict(self.left_pending_by_alpha),
            "reconciles": self.reconciles(),
        }
