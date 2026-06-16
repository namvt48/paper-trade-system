from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone

from base.engine import BaseEngine
from base.symbol_utils import get_binance_perp_symbols
from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import Selection, build_panel, select_positions

logger = logging.getLogger(__name__)


class CrossSectionalEngine(BaseEngine):
    """Portfolio engine for the Markdown cross-sectional alpha specifications."""

    def __init__(self, settings):
        super().__init__(settings)
        self.settings = settings
        self.spec = AlphaSpec.load(settings.SPEC_FILE)
        self._open_positions: dict[str, dict] = {}
        self._pending: tuple[int, Selection] | None = None
        self._last_processed_candle = 0
        self._portfolio_returns: list[float] = []
        self._last_prices: dict[str, float] = {}
        self._base_weights: dict[str, float] = {}
        self._pending_cost = 0.0
        self._strategy_leverage = 0.0
        self._columns_config_path = os.path.join(os.path.dirname(settings.SPEC_FILE), "config.toml")

    def get_required_channels(self) -> list[str]:
        return [f"kline:{self.spec.timeframe}"]

    def _get_warmup_symbols(self) -> list[str]:
        try:
            with open(self.settings.UNIVERSE_FILE, encoding="utf-8") as fh:
                configured = json.load(fh)["symbols"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            configured = []
        tradable = set(get_binance_perp_symbols())
        if configured and len(tradable) <= 2:
            # symbol_utils intentionally falls back to BTC/ETH on API failure;
            # retain the configured universe rather than collapsing the alpha.
            tradable = set(configured)
        symbols = [s for s in configured if (not tradable or s in tradable) and not self._is_blacklisted(s)]
        return symbols or [s for s in sorted(tradable) if not self._is_blacklisted(s)]

    def _has_open_positions(self) -> bool:
        return bool(self._open_positions)

    def on_position_reconciled(self, position: dict, mode: str) -> None:
        symbol = str(position.get("symbol", ""))
        weight = float(position.get("weight", 0.0) or 0.0)
        if symbol and weight:
            self._base_weights[symbol] = weight
        self._strategy_leverage = max(
            self._strategy_leverage,
            float(position.get("strategy_leverage", 0.0) or 0.0),
        )

    async def _manage_positions(self) -> None:
        # The specs explicitly have no TP, SL, time stop, or intrabar exits.
        return

    async def scan_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self._wait_until_next_candle_offset()
                await self._process_latest_candle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[%s] Scan failed: %s", self.spec.alpha_id, exc, exc_info=True)
                await asyncio.sleep(1)

    async def _wait_until_next_candle_offset(self) -> None:
        seconds = self._tf_to_seconds(self.spec.timeframe)
        now = time.time()
        target = (int(now // seconds) + 1) * seconds + self.settings.OFFSET_CANDLE_SEC
        await asyncio.sleep(max(0.0, target - now))

    async def _snapshot(self) -> dict:
        out = {}
        async with self.data_lock:
            for symbol, tf_map in self.symbol_data.items():
                sd = tf_map.get(self.spec.timeframe)
                if not sd or not sd.time_list:
                    continue
                out[symbol] = {
                    "time": list(sd.time_list),
                    "close": list(sd.price_list),
                    "high": list(sd.high_list),
                    "low": list(sd.low_list),
                    "volume": list(sd.volume_list),
                }
        return out

    async def _process_latest_candle(self) -> None:
        snapshot = await self._snapshot()
        if not snapshot:
            return
        latest = max(int(row["time"][-1]) for row in snapshot.values() if row["time"])
        if latest <= self._last_processed_candle:
            return

        prices = {symbol: float(row["close"][-1]) for symbol, row in snapshot.items() if row["close"]}
        self._record_portfolio_return(prices)

        if self._pending and latest >= self._pending[0]:
            _, selection = self._pending
            self._apply_selection(selection, prices, latest)
            self._pending = None

        bar_number = latest // self._tf_to_ms(self.spec.timeframe)
        is_rebalance = bar_number % self.spec.rebalance_bars == 0
        panel = build_panel(snapshot)
        selection = select_positions(panel, self.spec)
        self._logger.info(
            "[SIGNAL_AUDIT] %s",
            json.dumps({
                "alpha_id": self.spec.alpha_id,
                "timeframe": self.spec.timeframe,
                "signal_candle_open_ms": latest,
                "is_rebalance": is_rebalance,
                "signal": self.spec.signal,
                "params": self.spec.params,
                "long_threshold": self.spec.long_threshold,
                "short_threshold": self.spec.short_threshold,
                "symbols": selection.indicators,
            }, separators=(",", ":"), allow_nan=False),
        )
        if is_rebalance:
            execute_at = latest + self.spec.exec_lag * self._tf_to_ms(self.spec.timeframe)
            self._pending = (execute_at, selection)
            self._logger.info(
                "[%s] Decision queued for %d: long=%d short=%d gross=%.3f net=%.6f",
                self.spec.alpha_id, execute_at, len(selection.longs), len(selection.shorts),
                selection.diagnostics["gross"], selection.diagnostics["net"],
            )

        self._last_prices = prices
        self._last_processed_candle = latest

    def _record_portfolio_return(self, prices: dict[str, float]) -> None:
        if not self._last_prices or not self._base_weights:
            return
        gross = 0.0
        for symbol, weight in self._base_weights.items():
            before = self._last_prices.get(symbol)
            after = prices.get(symbol)
            if before and after:
                gross += weight * (after / before - 1.0)
        self._portfolio_returns.append(gross - self._pending_cost)
        self._pending_cost = 0.0
        self._portfolio_returns = self._portfolio_returns[-max(self.spec.vol_lookback * 2, 10):]

    def _vol_target_leverage(self) -> float:
        minimum = max(2, self.spec.vol_lookback // 2)
        values = self._portfolio_returns[-self.spec.vol_lookback:]
        if len(values) < minimum:
            return self._strategy_leverage
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
        rv = math.sqrt(variance) * math.sqrt(self.spec.ppy)
        if rv <= 0:
            return 0.0
        return min(self.spec.max_leverage, self.spec.target_vol / rv)

    def _apply_selection(self, selection: Selection, prices: dict[str, float], candle_open_ms: int) -> None:
        # Quantity modification is not supported by the paper worker. Closing and
        # reopening the rebalance basket preserves target quantities and sides.
        for symbol, pos in list(self._open_positions.items()):
            self.push_signal("CLOSE", position_id=pos["position_id"], exit_price=prices.get(symbol, pos["entry"]), reason="REBALANCE")
            self._open_positions.pop(symbol, None)

        symbols = set(self._base_weights) | set(selection.weights)
        turnover = sum(abs(selection.weights.get(symbol, 0.0) - self._base_weights.get(symbol, 0.0)) for symbol in symbols)
        self._pending_cost = turnover * self.spec.fee_bps / 10_000
        self._strategy_leverage = self._vol_target_leverage()
        self._base_weights = dict(selection.weights)
        if self._strategy_leverage <= 0 or not self.can_open_new_trades():
            self.mark_positions_changed()
            return

        for symbol, weight in selection.weights.items():
            price = prices.get(symbol)
            if not price or price <= 0:
                continue
            side = "LONG" if weight > 0 else "SHORT"
            notional = self.settings.CAPITAL * abs(weight) * self._strategy_leverage
            position_id = str(uuid.uuid4())
            pos = {
                "position_id": position_id,
                "symbol": symbol,
                "side": side,
                "entry": price,
                "qty": notional / price,
                "weight": weight,
                "strategy_leverage": self._strategy_leverage,
                "entry_candle_open_ms": candle_open_ms,
            }
            self._open_positions[symbol] = pos
            self.push_signal(
                "OPEN",
                symbol=symbol,
                side=side,
                entry=price,
                qty=pos["qty"],
                leverage=1,
                position_id=position_id,
                exchange=self.settings.EXCHANGE,
                fee_pct=self.spec.fee_bps / 10_000,
                metadata=json.dumps({
                    "score": selection.scores.get(symbol),
                    "rank": selection.ranks.get(symbol),
                    "weight": weight,
                    "strategy_leverage": self._strategy_leverage,
                    **selection.diagnostics,
                }),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        self.mark_positions_changed()
