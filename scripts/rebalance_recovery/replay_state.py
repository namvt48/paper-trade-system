"""State loading and deterministic identifiers for historical replay."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .configuration import RecoveryAlphaConfig
from .domain import AlphaId, PositionId, RecoveryPoint, Side, SignalId

POSITION_NAMESPACE = uuid.UUID("60ddb935-9cce-4bf4-b701-a82d2b45d9fc")


@dataclass(frozen=True, slots=True)
class PositionState:
    """Minimal runner position state needed across consecutive replay events."""

    position_id: PositionId
    symbol: str
    side: Side
    qty: float
    fee_pct: float
    strategy_leverage: float
    entry_price: float


class ReplayError(Exception):
    """Frozen inputs cannot reproduce a requested rebalance."""


def load_position_state(baseline_db: Path) -> dict[AlphaId, dict[str, PositionState]]:
    """Seed replay from the open book captured in the baseline database."""
    state: dict[AlphaId, dict[str, PositionState]] = {}
    with sqlite3.connect(f"file:{baseline_db.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM positions"):
            metadata = json.loads(row["metadata"] or "{}")
            leverage = (
                float(metadata.get("strategy_leverage", 1.0))
                if isinstance(metadata, dict)
                else 1.0
            )
            alpha_id = AlphaId(row["alpha_id"])
            state.setdefault(alpha_id, {})[row["symbol"]] = PositionState(
                position_id=PositionId(row["position_id"]),
                symbol=row["symbol"],
                side=Side(row["side"]),
                qty=float(row["qty"]),
                fee_pct=float(row["fee_pct"] or 0.0),
                strategy_leverage=leverage,
                entry_price=(
                    float(metadata.get("decision_price", row["entry_price"]))
                    if isinstance(metadata, dict)
                    else float(row["entry_price"])
                ),
            )
    return state


def assert_points_are_missing(
    baseline_db: Path, points: tuple[RecoveryPoint, ...]
) -> None:
    """Reject any target cycle containing signals instead of guessing partial state."""
    existing: set[tuple[str, int]] = set()
    with sqlite3.connect(f"file:{baseline_db.resolve()}?mode=ro", uri=True) as conn:
        for alpha_id, payload_raw in conn.execute(
            "SELECT alpha_id, payload FROM signals"
        ):
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                continue
            candle = (
                payload.get("signal_candle_open_ms")
                if isinstance(payload, dict)
                else None
            )
            if candle is not None:
                existing.add((alpha_id, int(candle)))
    conflicts = [
        point
        for point in points
        if (str(point.alpha_id), int(point.candle_open_ms)) in existing
    ]
    if conflicts:
        detail = ", ".join(
            f"{point.alpha_id}@{int(point.candle_open_ms)}" for point in conflicts
        )
        raise ReplayError(f"target cycles already contain signals: {detail}")


def strategy_leverage(current: dict[str, PositionState]) -> float:
    """Reuse persisted leverage when available, otherwise runner default 1.0."""
    return next(iter(current.values())).strategy_leverage if current else 1.0


def position_id(point: RecoveryPoint, symbol: str, side: Side) -> PositionId:
    """Derive an idempotent position identifier independent of recovery run ID."""
    logical = f"{point.alpha_id}|{int(point.candle_open_ms)}|{symbol}|{side.value}"
    return PositionId(str(uuid.uuid5(POSITION_NAMESPACE, logical)))


def signal_id(
    config: RecoveryAlphaConfig,
    kind: str,
    symbol: str,
    side: Side,
    target_position_id: PositionId,
    point: RecoveryPoint,
) -> SignalId:
    """Match the runner dispatcher's stable logical signal hash."""
    logical = {
        "alpha_id": str(config.alpha_id),
        "version": config.version,
        "type": kind,
        "symbol": symbol,
        "tf": point.timeframe,
        "side": side.value if kind == "OPEN" else "",
        "position_id": str(target_position_id),
        "signal_candle_open_ms": int(point.candle_open_ms),
        "reason": "REBALANCE" if kind == "CLOSE" else "",
    }
    raw = json.dumps(logical, sort_keys=True, separators=(",", ":"))
    return SignalId(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32])
