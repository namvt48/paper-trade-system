"""Replay frozen cross-sectional inputs into deterministic close/open ledger entries."""

from __future__ import annotations

import json
from pathlib import Path

from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import (
    CrossAlphaComputeContext,
    Selection,
    build_funding_panel,
    build_panel,
    resample_funding_to_native_cadence,
    select_positions,
)
from indicators.pandas.ts_ops import ts_zscore

from .configuration import RecoveryAlphaConfig, load_symbols
from .domain import (
    CloseLedgerEntry,
    LedgerEntry,
    OpenLedgerEntry,
    RecoveryPoint,
    Side,
)
from .replay_state import (
    PositionState,
    assert_points_are_missing,
    load_position_state,
    position_id,
    signal_id,
    strategy_leverage,
)


def build_recovery_ledger(
    baseline_db: Path,
    market_root: Path,
    alphas_dir: Path,
    configs: tuple[RecoveryAlphaConfig, ...],
    points: tuple[RecoveryPoint, ...],
) -> tuple[LedgerEntry, ...]:
    """Replay missing points chronologically without writing any database."""
    assert_points_are_missing(baseline_db, points)
    pending = points
    state = load_position_state(baseline_db)
    by_alpha = {config.alpha_id: config for config in configs}
    entries: list[LedgerEntry] = []
    for point in pending:
        config = by_alpha[point.alpha_id]
        selection, prices = _select(config, point, market_root, alphas_dir)
        current = state.setdefault(point.alpha_id, {})
        leverage = strategy_leverage(current)
        for symbol, position in sorted(current.items()):
            decision_price = prices.get(symbol, position.entry_price)
            entries.append(
                CloseLedgerEntry(
                    alpha_id=point.alpha_id,
                    signal_id=signal_id(
                        config,
                        "CLOSE",
                        symbol,
                        position.side,
                        position.position_id,
                        point,
                    ),
                    position_id=position.position_id,
                    symbol=symbol,
                    side=position.side,
                    decision_price=decision_price,
                    candle_open_ms=point.candle_open_ms,
                    event_at=point.event_at,
                    fee_pct=position.fee_pct,
                )
            )
        current.clear()
        weights = _tradable_balanced_weights(selection, prices)
        spec = AlphaSpec.load(config.spec_path)
        for symbol, weight in sorted(weights.items()):
            side = Side.LONG if weight > 0 else Side.SHORT
            decision_price = prices[symbol]
            target_position_id = position_id(point, symbol, side)
            qty = config.capital * abs(weight) * leverage / decision_price
            target_signal_id = signal_id(
                config, "OPEN", symbol, side, target_position_id, point
            )
            entries.append(
                OpenLedgerEntry(
                    alpha_id=point.alpha_id,
                    signal_id=target_signal_id,
                    position_id=target_position_id,
                    symbol=symbol,
                    side=side,
                    decision_price=decision_price,
                    qty=qty,
                    weight=weight,
                    strategy_leverage=leverage,
                    candle_open_ms=point.candle_open_ms,
                    event_at=point.event_at,
                    fee_pct=spec.fee_bps / 10_000.0,
                )
            )
            current[symbol] = PositionState(
                position_id=target_position_id,
                symbol=symbol,
                side=side,
                qty=qty,
                fee_pct=spec.fee_bps / 10_000.0,
                strategy_leverage=leverage,
                entry_price=decision_price,
            )
    return tuple(entries)


def _select(
    config: RecoveryAlphaConfig,
    point: RecoveryPoint,
    market_root: Path,
    alphas_dir: Path,
) -> tuple[Selection, dict[str, float]]:
    """Run the production selection function against a candle-capped panel."""
    snapshot = {}
    for symbol in load_symbols(config):
        candle_path = market_root / point.timeframe / f"{symbol}.json"
        if not candle_path.exists():
            continue
        rows = json.loads(candle_path.read_text(encoding="utf-8"))
        rows = [
            row for row in rows if int(row["open_time"]) <= int(point.candle_open_ms)
        ]
        snapshot[symbol] = {
            "time": [int(row["open_time"]) for row in rows],
            "close": [float(row["close"]) for row in rows],
            "high": [float(row["high"]) for row in rows],
            "low": [float(row["low"]) for row in rows],
            "volume": [float(row["volume"]) for row in rows],
        }
    panel = build_panel(snapshot)
    spec = AlphaSpec.load(config.spec_path)
    if spec.needs_funding:
        funding_snapshot = {}
        for symbol in load_symbols(config):
            funding_path = market_root / "funding" / f"{symbol}.json"
            if not funding_path.exists():
                continue
            funding_snapshot[symbol] = [
                row
                for row in json.loads(funding_path.read_text(encoding="utf-8"))
                if int(row["funding_time"]) <= int(point.event_at.timestamp() * 1000)
            ]
        funding = resample_funding_to_native_cadence(
            build_funding_panel(funding_snapshot)
        )
        panel["funding_zscore"] = ts_zscore(
            funding,
            int(spec.params.get("funding_window", 21)),
        ).reindex(panel["close"].index, method="ffill")
    member_specs = None
    if spec.signal == "ensemble_mean" and spec.members:
        member_specs = [
            AlphaSpec.load(alphas_dir / member / "spec.json") for member in spec.members
        ]
    selection = select_positions(
        panel,
        spec,
        context=CrossAlphaComputeContext(panel),
        member_specs=member_specs,
        current_drawdown=0.0,
    )
    if spec.reverse:
        selection = Selection(
            longs=selection.shorts,
            shorts=selection.longs,
            scores=selection.scores,
            ranks=selection.ranks,
            weights={symbol: -weight for symbol, weight in selection.weights.items()},
            indicators=selection.indicators,
            diagnostics=selection.diagnostics,
        )
    prices = {
        str(symbol): float(price)
        for symbol, price in panel["close"].ffill().iloc[-1].dropna().items()
    }
    return selection, prices


def _tradable_balanced_weights(
    selection: Selection, prices: dict[str, float]
) -> dict[str, float]:
    """Mirror runner price filtering and equal LONG/SHORT count trimming."""
    weights = {
        symbol: weight
        for symbol, weight in selection.weights.items()
        if prices.get(symbol, 0.0) > 0.0
    }
    longs = [symbol for symbol, weight in weights.items() if weight > 0]
    shorts = [symbol for symbol, weight in weights.items() if weight < 0]
    target = min(len(longs), len(shorts))
    if target == 0:
        return weights
    drop_longs = sorted(longs, key=lambda symbol: weights[symbol])[
        : len(longs) - target
    ]
    drop_shorts = sorted(shorts, key=lambda symbol: abs(weights[symbol]))[
        : len(shorts) - target
    ]
    dropped = set(drop_longs + drop_shorts)
    return {
        symbol: weight for symbol, weight in weights.items() if symbol not in dropped
    }
