from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from runner.strategy.context import StrategyContext

logger = logging.getLogger(__name__)


def merge_authoritative_positions(
    runner_store: dict[str, dict], authoritative: dict[str, dict]
) -> tuple[dict[str, dict], list[str], list[str]]:
    """Reconcile the runner-side position cache against the worker's authoritative set.

    ``runner_store``  : ``{symbol: engine_native_position}`` (runner:positions cache).
    ``authoritative`` : ``{position_id: normalized_position}`` (worker DB snapshot).

    The worker DB decides *which* positions are open; the runner cache carries the
    engine's richer runtime (milestone/trail/weight). Returns
    ``(reconciled, adopted_from_snapshot, dropped_phantoms)`` where ``reconciled`` is
    ``{symbol: position}`` covering exactly the authoritative position_ids: keep the
    runner cache's entry when the id matches (runtime preserved), else adopt the
    snapshot's normalized position. Runner-cache positions the DB does not know about
    are dropped -- they would only ever be rejected as duplicates.
    """
    runner_by_id: dict[str, tuple[str, dict]] = {}
    if isinstance(runner_store, dict):
        for symbol, pos in runner_store.items():
            if isinstance(pos, dict) and pos.get("position_id"):
                runner_by_id[str(pos["position_id"])] = (str(symbol), pos)

    reconciled: dict[str, dict] = {}
    adopted: list[str] = []
    for position_id, normalized in authoritative.items():
        match = runner_by_id.get(position_id)
        if match is not None:
            symbol, pos = match
            reconciled[symbol] = pos
        else:
            symbol = normalized.get("symbol")
            if symbol:
                reconciled[symbol] = normalized
                adopted.append(position_id)
    dropped = [pid for pid in runner_by_id if pid not in authoritative]
    return reconciled, adopted, dropped


class Strategy(ABC):
    def __init__(self, alpha_id: str, version: str, params: dict, ctx: StrategyContext):
        self.alpha_id = alpha_id
        self.version = version
        self.params = params
        self.ctx = ctx

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        return ["kline:1m"]

    @abstractmethod
    def get_required_channels_instance(self) -> list[str]:
        ...

    @abstractmethod
    def get_warmup_symbols(self) -> list[str]:
        ...

    @abstractmethod
    def get_warmup_tfs(self) -> list[str]:
        ...

    @abstractmethod
    def get_warmup_bars(self, tf: str) -> int:
        ...

    def get_retain_bars(self, tf: str) -> int:
        return self.get_warmup_bars(tf)

    def get_retain_buffer_bars(self, tf: str) -> int:
        return 0

    async def on_candle(self, symbol: str, tf: str) -> None:
        return None

    async def on_price_alert(self, symbol: str, price: float, side: str) -> None:
        return None

    def should_scan_after_event(self, kind: str, symbol: str | None = None, tf: str | None = None) -> bool:
        return True

    async def scan(self) -> None:
        return None

    async def manage_positions(self) -> None:
        return None

    def reconcile_open_positions(self) -> dict:
        """Adopt the worker's authoritative open-position set on startup.

        The runner-side ``runner:positions`` cache can silently diverge from the worker
        DB across restarts, orphaning a DB position the engine no longer manages (never
        closed) while every new OPEN on that symbol is rejected by the worker's duplicate
        policy (2026-07-02 ``deobietcophaialphakhong`` incident). The worker snapshot is
        authoritative for *which* positions are open; the runner cache is kept only for
        its richer runtime when a position_id still matches. Used by both the
        legacy-standalone and cross-sectional runner strategies.
        """
        runner_store = self.ctx.load_positions()
        authoritative = self.ctx.load_authoritative_positions()
        if authoritative is None:
            # Worker snapshot unavailable (worker down / brand-new alpha): fall back to
            # the runner cache rather than wiping live state.
            return runner_store if isinstance(runner_store, dict) else {}
        reconciled, adopted, dropped = merge_authoritative_positions(
            runner_store, authoritative
        )
        if adopted or dropped:
            logger.warning(
                "[STARTUP-RECONCILE] alpha=%s authoritative=%d "
                "adopted_from_worker_snapshot=%s dropped_phantom=%s -- runner cache had "
                "diverged from worker DB",
                self.alpha_id, len(authoritative), adopted, dropped,
                extra={"alpha_id": self.alpha_id},
            )
        return reconciled
