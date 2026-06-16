from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StrategyRuntimeState:
    data_stale: bool = False
    reconcile_stale: bool = False
    price_alert_stale: bool = False
    lease_valid: bool = True
    ready: bool = False
    dropped_events: int = 0

    def can_open_new_trades(self) -> bool:
        return (
            self.ready
            and self.lease_valid
            and not self.data_stale
            and not self.reconcile_stale
            and not self.price_alert_stale
        )

    def can_manage_existing_positions(self) -> bool:
        return self.ready and self.lease_valid

    def mark_no_positions_ok(self) -> None:
        self.reconcile_stale = False

    def mark_stale_snapshot(self, has_positions: bool) -> None:
        self.reconcile_stale = bool(has_positions)

    def mark_redis_error(self) -> None:
        self.reconcile_stale = True

    def mark_reconcile_ok(self) -> None:
        self.reconcile_stale = False

