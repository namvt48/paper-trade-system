from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone

from pathlib import Path

from base.engine import BaseEngine
from base import signal_push
from base.symbol_utils import get_binance_perp_symbols
from cross_alpha.schedule import is_midnight_close_utc, is_rebalance_due
from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import Selection, build_panel, select_positions
from portfolio_manager.core.book import TargetBook, TargetBookStore

logger = logging.getLogger(__name__)


class CrossSectionalEngine(BaseEngine):
    """Portfolio engine for the Markdown cross-sectional alpha specifications."""

    def __init__(self, settings):
        super().__init__(settings)
        self.settings = settings
        self.spec = AlphaSpec.load(settings.SPEC_FILE)
        self._open_positions: dict[str, dict] = {}
        self._last_processed_candle = 0
        self._portfolio_returns: list[float] = []
        self._last_prices: dict[str, float] = {}
        self._base_weights: dict[str, float] = {}
        self._pending_cost = 0.0
        self._strategy_leverage = 0.0
        self._last_pnl_publish: dict[str, float] = {}
        self._pnl_channel = f"pnl:{self.alpha_id}"
        self.book_only = bool(getattr(self.spec, "book_only", False) or getattr(settings, "BOOK_ONLY", False))
        self._book_revision = 0
        self._book_store = TargetBookStore(signal_push._r) if self.book_only and signal_push._r is not None else None
        self._columns_config_path = os.path.join(os.path.dirname(settings.SPEC_FILE), "config.toml")
        # Peak-equity / drawdown tracking, driving the ensemble overlay's
        # drawdown_throttle step (see cross_alpha/overlay.py). Kept here
        # (stateful, per-alpha) rather than in strategy.py/overlay.py, which
        # stay pure functions over a panel.
        self._equity = 1.0
        self._peak_equity = 1.0
        self._member_specs: list[AlphaSpec] | None = None
        if self.spec.signal == "ensemble_mean" and self.spec.members:
            alphas_root = Path(settings.SPEC_FILE).resolve().parents[1]
            self._member_specs = [
                AlphaSpec.load(alphas_root / member_id / "spec.json")
                for member_id in self.spec.members
            ]

    def get_required_channels(self) -> list[str]:
        return [f"kline:{self.spec.timeframe}"]

    def _get_warmup_symbols(self) -> list[str]:
        if self._whitelist:
            return sorted(s for s in self._whitelist if not self._is_blacklisted(s))
        tradable = get_binance_perp_symbols()
        return [s for s in tradable if not self._is_blacklisted(s)]

    def _has_open_positions(self) -> bool:
        return bool(self._open_positions)

    async def on_warmup_complete(self) -> None:
        """Bootstrap portfolio returns from historical candles so vol is estimated on first live candle."""
        deadline = time.time() + float(getattr(self.settings, "INITIAL_DATA_TIMEOUT_SEC", 300.0))
        while not self.symbol_data and time.time() < deadline:
            await asyncio.sleep(1)
        if not self.symbol_data:
            await asyncio.sleep(5)

        snapshot = await self._snapshot()
        if not snapshot:
            self._logger.warning(
                "[%s] Vol bootstrap: no snapshot data (symbol_data=%d)",
                self.alpha_id, len(self.symbol_data),
            )
            return

        panel = build_panel(snapshot)
        close_df = panel["close"]
        n_bars = len(close_df)
        self._logger.info(
            "[%s] Vol bootstrap: symbol_data=%d snapshot=%d bars=%d",
            self.alpha_id, len(self.symbol_data), len(snapshot), n_bars,
        )
        if n_bars < 3:
            return

        start_bar = max(self.spec.required_bars, 3)
        replay_bars = n_bars - start_bar
        if replay_bars < 2:
            self._logger.info(
                "[%s] Vol bootstrap skipped: %d replay bars (bars=%d, required=%d)",
                self.alpha_id, replay_bars, n_bars, self.spec.required_bars,
            )
            return

        prev_weights: dict[str, float] = {}
        prev_prices: dict[str, float] = {}
        bootstrapped = 0

        for i in range(start_bar, n_bars):
            sub_panel = {k: v.iloc[: i + 1] for k, v in panel.items()}
            try:
                selection = select_positions(
                    sub_panel, self.spec,
                    member_specs=self._member_specs, current_drawdown=self._current_drawdown(),
                )
            except Exception:
                continue

            curr_prices: dict[str, float] = {}
            row = close_df.iloc[i]
            for symbol in close_df.columns:
                val = row.get(symbol)
                if val is not None and not (isinstance(val, float) and math.isnan(val)):
                    curr_prices[str(symbol)] = float(val)

            if prev_weights and prev_prices:
                gross = 0.0
                for symbol, weight in prev_weights.items():
                    before = prev_prices.get(symbol)
                    after = curr_prices.get(symbol)
                    if before and after:
                        gross += weight * (after / before - 1.0)
                self._portfolio_returns.append(gross)
                bootstrapped += 1

            prev_weights = dict(selection.weights)
            prev_prices = curr_prices

        self._base_weights = prev_weights
        self._last_prices = prev_prices
        self._strategy_leverage = self.spec.max_leverage

        self._logger.info(
            "[%s] Vol bootstrap done: %d returns, leverage=%.4f (fixed=max_leverage)",
            self.alpha_id, bootstrapped, self._strategy_leverage,
        )

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

    def on_price_alert_message(self, msg: dict) -> None:
        symbol = str(msg.get("symbol", ""))
        pos = self._open_positions.get(symbol)
        if pos is None:
            return
        pos_side = str(pos.get("side", ""))
        price = self._trigger_price(pos_side, msg)
        if price is None or price <= 0:
            return
        now = time.time()
        if now - self._last_pnl_publish.get(symbol, 0.0) < 0.5:
            return
        entry = float(pos.get("entry", 0.0))
        if entry <= 0:
            return
        if pos_side == "LONG":
            pnl_pct = (price - entry) / entry
        elif pos_side == "SHORT":
            pnl_pct = (entry - price) / entry
        else:
            return
        payload = json.dumps({
            "alpha_id": self.alpha_id,
            "symbol": symbol,
            "side": pos_side,
            "entry_price": entry,
            "current_price": price,
            "pnl_pct": round(pnl_pct, 6),
            "weight": float(pos.get("weight", 0.0)),
            "timestamp": int(now * 1000),
        })
        from base import signal_push
        if signal_push._r is not None:
            try:
                signal_push._r.publish(self._pnl_channel, payload)
            except Exception:
                logger.debug("[PNL] publish failed for %s", symbol)
        self._last_pnl_publish[symbol] = now

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

        tf_ms = self._tf_to_ms(self.spec.timeframe)
        if self.spec.publish_at_midnight_utc:
            if not is_midnight_close_utc(latest, tf_ms):
                self._logger.debug(
                    "[%s] scan SKIP: waiting for 00:00 UTC close (candle_open=%d)",
                    self.config.ALPHA_ID, latest,
                )
                self._last_prices = prices
                self._last_processed_candle = latest
                return
        is_rebalance = is_rebalance_due(
            latest,
            tf_ms,
            self.spec.rebalance_bars,
            publish_at_midnight_utc=self.spec.publish_at_midnight_utc,
            rebalance_on_close=getattr(self.spec, "rebalance_on_close", False),
        )
        panel = build_panel(snapshot)

        # DIAGNOSTIC: log panel shape and NaN counts before signal computation
        close_df = panel["close"]
        self._logger.info(
            "[DIAG] panel shape=%s symbols=%d total_nan_close=%d rows_with_all_nan=%d",
            close_df.shape, len(close_df.columns), int(close_df.isna().sum().sum()),
            int((close_df.notna().sum(axis=1) == 0).sum()),
        )
        if close_df.shape[0] > 1920:
            row_neg1921 = close_df.iloc[-1921]
            self._logger.info(
                "[DIAG] row[-1921] valid=%d/%d sample_nan=%s",
                int(row_neg1921.notna().sum()), len(row_neg1921),
                sorted(row_neg1921[row_neg1921.isna()].index.tolist())[:5],
            )

        selection = select_positions(
            panel, self.spec,
            member_specs=self._member_specs, current_drawdown=self._current_drawdown(),
        )
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
        self._apply_selection(selection, prices, latest)
        self._logger.info(
            "[%s] Decision applied at %d: long=%d short=%d gross=%.3f net=%.6f rebalance=%s",
            self.spec.alpha_id, latest, len(selection.longs), len(selection.shorts),
            selection.diagnostics["gross"], selection.diagnostics["net"], is_rebalance,
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
        ret = gross - self._pending_cost
        self._portfolio_returns.append(ret)
        self._pending_cost = 0.0
        self._portfolio_returns = self._portfolio_returns[-max(self.spec.vol_lookback * 2, 10):]
        self._equity *= (1.0 + ret)
        if self._equity > self._peak_equity:
            self._peak_equity = self._equity

    def _current_drawdown(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return (self._equity - self._peak_equity) / self._peak_equity

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
        if self.book_only:
            self._base_weights = dict(selection.weights)
            self._publish_target_book(selection, prices, candle_open_ms)
            self._open_positions.clear()
            self.mark_positions_changed()
            return
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
            if self._whitelist and symbol not in self._whitelist:
                continue
            if self._is_blacklisted(symbol):
                continue
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

    def _publish_target_book(self, selection: Selection, prices: dict[str, float], candle_open_ms: int) -> None:
        if self._book_store is None:
            logger.error("[%s] BOOK_ONLY configured but Redis is unavailable", self.alpha_id)
            return
        self._book_revision += 1
        book = TargetBook.create(
            self.alpha_id,
            self.spec.timeframe,
            selection.weights,
            revision=self._book_revision,
            as_of_candle_ms=candle_open_ms,
            meta={
                "n_long": len(selection.longs),
                "n_short": len(selection.shorts),
                "book_only": True,
                "prices": {symbol: float(prices[symbol]) for symbol in selection.weights if symbol in prices},
                "diagnostics": selection.diagnostics,
            },
        )
        self._book_store.write(book)
