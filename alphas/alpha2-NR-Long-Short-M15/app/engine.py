"""Paper-trade engine for the alpha2-NR-Long-Short reversal strategy.

Logic
-----
* indi1 = EMA cross (fast/slow). Green when ``ema_fast > ema_slow``.
* indi2 = Hull Butterfly oscillator. Green when ``hso > 0``.
* Entry : both green => LONG, both red => SHORT.
* Exit  : the open position is closed and reversed when the two indicators
  align in the opposite direction. No TP/SL — exits are purely signal-driven.

Trades BTCUSDT only, on a single timeframe configured via ``settings.TF``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import (
    combined_signal,
    ema_cross_color,
    hull_butterfly_color,
)
from base.engine import BaseEngine

logger = logging.getLogger(__name__)

SYMBOL = "BTCUSDT"


def _candle_seconds(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    return 3600


def _calc_net_pnl(side: str, entry: float, exit_price: float, size: float, fee_rate: float) -> float:
    qty = size / entry
    gross = qty * (exit_price - entry) if side == "LONG" else qty * (entry - exit_price)
    fee_in = fee_rate * size
    fee_out = fee_rate * (qty * exit_price)
    return gross - fee_in - fee_out


class Alpha2NRLongShortEngine(BaseEngine):
    """Dual-indicator reversal alpha (indi1 EMA cross + indi2 Hull Butterfly)."""

    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}
        self._cur_eq: float = settings.CAPITAL
        self._cur_size: float = settings.INVEST_PER_TRADE

    # ── BaseEngine interface ─────────────────────────────────────────────────

    def get_required_channels(self) -> list[str]:
        return [f"kline:{settings.TF}"]

    def _get_warmup_symbols(self) -> list[str]:
        return [SYMBOL]

    def _has_open_positions(self) -> bool:
        return len(self._open_positions) > 0

    async def _manage_positions(self) -> None:
        # Pure signal-reversal strategy: positions are closed only by the
        # opposite alignment signal in _apply_decision. No TP/SL management.
        pass

    async def on_warmup_complete(self) -> None:
        logger.info(
            "[%s] Warmup complete. tf=%s symbol=%s",
            settings.ALPHA_ID, settings.TF, SYMBOL,
        )

    async def scan_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self._wait_until_next_candle_offset()
                if self.shutdown_event.is_set():
                    break
                await self._scan_symbol()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scan error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    # ── Scanning ─────────────────────────────────────────────────────────────

    async def _wait_until_next_candle_offset(self) -> None:
        candle_len = _candle_seconds(settings.TF)
        now = time.time()
        next_candle = (int(now // candle_len) + 1) * candle_len
        target = next_candle + settings.OFFSET_CANDLE_SEC
        wait = target - now
        if wait > 0:
            await asyncio.sleep(wait)

    async def _scan_symbol(self) -> None:
        row = None
        async with self.data_lock:
            row = self._build_symbol_row(SYMBOL)
        if row is None:
            return
        indic = self._compute_indicators(row)
        if indic is not None:
            self._apply_decision(row, indic)

    def _build_symbol_row(self, symbol: str) -> dict | None:
        tf_map = self.symbol_data.get(symbol)
        if not tf_map:
            return None
        sd = tf_map.get(settings.TF)
        if not sd or not sd.price_list or not sd.time_list:
            return None
        return {
            "symbol": symbol,
            "close_list": sd.price_list,
            "time_list": sd.time_list,
        }

    def _compute_indicators(self, row: dict) -> dict | None:
        """Pure indicator computation — thread-safe, no side effects."""
        close_arr = np.asarray(row["close_list"], dtype=float)
        times = row["time_list"]
        min_bars = max(settings.EMA_SLOW * 3, settings.HULL_LENGTH * 2)
        if len(close_arr) < min_bars:
            return None

        close_series = pd.Series(close_arr)
        c1 = ema_cross_color(close_series, settings.EMA_FAST, settings.EMA_SLOW)
        c2 = hull_butterfly_color(close_arr, settings.HULL_LENGTH)
        signal = combined_signal(c1, c2)

        ema_fast = float(close_series.ewm(span=settings.EMA_FAST, adjust=False).mean().iloc[-1])
        ema_slow = float(close_series.ewm(span=settings.EMA_SLOW, adjust=False).mean().iloc[-1])

        return {
            "latest_signal": str(signal[-1]),
            "indi1_color": str(c1[-1]),
            "indi2_color": str(c2[-1]),
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "close": float(close_arr[-1]),
            "signal_open_time_ms": int(times[-1]),
        }

    # ── Decision ─────────────────────────────────────────────────────────────

    def _apply_decision(self, row: dict, indic: dict) -> None:
        """Apply trading decisions — mutates state, must run sequentially."""
        symbol = row["symbol"]
        sig = indic["latest_signal"]
        close = indic["close"]
        signal_open_time_ms = indic["signal_open_time_ms"]

        pos = self._open_positions.get(symbol)
        current_pos = (
            1 if (pos and pos["side"] == "LONG")
            else -1 if (pos and pos["side"] == "SHORT")
            else 0
        )

        action = "HOLD"
        side = "FLAT"
        if sig == "long" and current_pos <= 0:
            action = "REVERSE->LONG" if current_pos < 0 else "OPEN_LONG"
            side = "LONG"
        elif sig == "short" and current_pos >= 0:
            action = "REVERSE->SHORT" if current_pos > 0 else "OPEN_SHORT"
            side = "SHORT"

        if action == "HOLD":
            return

        if pos:
            self._close_position(symbol, pos, close, "REVERSE", f"action={action}")
        self._open_new_position(symbol, side, close, signal_open_time_ms, indic)

    def _open_new_position(
        self,
        symbol: str,
        side: str,
        close: float,
        signal_open_time_ms: int,
        indic: dict,
    ) -> None:
        if not self.can_open_new_trades():
            return

        trade_size = self._cur_size
        symbol_lev = settings.LEVERAGE
        qty = trade_size / close
        position_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        signal_candle_close_ms = signal_open_time_ms + _candle_seconds(settings.TF) * 1000

        # No TP/SL — exits happen only via the opposite-signal CLOSE in
        # _apply_decision. tp/sl are omitted from the OPEN signal so the
        # worker stores NULL and never auto-closes on price.
        self._open_positions[symbol] = {
            "position_id": position_id,
            "side": side,
            "entry": close,
            "sl": 0.0,
            "tp": 0.0,
            "size": trade_size,
            "entry_candle_open_ms": signal_open_time_ms,
            "signal_candle_close_ms": signal_candle_close_ms,
        }
        self.mark_positions_changed()

        self.push_signal(
            "OPEN",
            symbol=symbol,
            side=side,
            entry=close,
            qty=qty,
            leverage=symbol_lev,
            position_id=position_id,
            exchange=getattr(settings, "EXCHANGE", "binance"),
            fee_pct=settings.FEE_PCT,
            metadata=json.dumps({
                "tf": settings.TF,
                "indi1_color": indic["indi1_color"],
                "indi2_color": indic["indi2_color"],
                "signal": indic["latest_signal"],
                "ema_fast": round(indic["ema_fast"], 2),
                "ema_slow": round(indic["ema_slow"], 2),
                "trade_size": round(trade_size, 2),
                "leverage": symbol_lev,
                "margin": round(trade_size / symbol_lev, 2),
                "cur_equity": round(self._cur_eq, 2),
            }),
            timestamp=timestamp,
        )
        logger.info(
            "[%s][OPEN] %s %s @ %.2f size=%.2f lev=%dx indi1=%s indi2=%s",
            settings.ALPHA_ID, side, symbol, close, trade_size, symbol_lev,
            indic["indi1_color"], indic["indi2_color"],
        )

    def _close_position(
        self,
        symbol: str,
        pos: dict,
        exit_price: float,
        reason: str,
        detail: str = "",
    ) -> None:
        self.push_signal(
            "CLOSE",
            position_id=pos["position_id"],
            exit_price=exit_price,
            reason=reason,
            metadata=json.dumps({"detail": detail}),
        )
        self._open_positions.pop(symbol, None)
        self.mark_positions_changed()

        trade_size = pos.get("size", self._cur_size)
        net = _calc_net_pnl(pos["side"], pos["entry"], exit_price, trade_size, settings.FEE_PCT)
        self._cur_eq += net
        logger.info(
            "[%s][CLOSE] %s reason=%s @ %.2f net=%.2f equity=%.2f",
            settings.ALPHA_ID, symbol, reason, exit_price, net, self._cur_eq,
        )

    def on_price_alert_message(self, msg: dict) -> None:
        pass
