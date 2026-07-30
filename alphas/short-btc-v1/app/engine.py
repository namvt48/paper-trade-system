"""short-btc-v1 execution engine.

Ported from market-data-service/docs/backtest_v2.py's V2_CLV_DG_CONTEXT_EXIT
strategy: EMA downtrend + RSI + CLV breakdown entry, D1 EMA gate, ATR SL / R:R
TP / time-stop exit, and a funding+OI context-sized partial exit ("reduce").
DCA is intentionally NOT ported — the paper-trade signal protocol has no
average-entry MODIFY, and DCA was disabled by default in the source alpha too
(see plan notes).

Threading contract (enforced by runner.strategies.legacy_standalone): the
runner calls `_compute_indicators` via `asyncio.to_thread` (safe for blocking
I/O) and then `_apply_decision` directly on the event loop (must NOT block).
All MDS Redis reads therefore happen inside `_compute_indicators`.
"""

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import redis as redis_lib

from app.config import settings
from app.strategy import (
    calc_atr,
    calc_ema,
    calc_rsi,
    compute_context_exit_fraction,
    compute_entry_signal,
    passes_d1_downtrend,
    read_last_at_or_before,
    read_last_completed_daily,
)
from base.engine import BaseEngine

logger = logging.getLogger(__name__)


def _candle_seconds(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    if tf.endswith("d"):
        return int(tf[:-1]) * 86400
    return 60


def _calc_net_pnl(entry: float, exit_price: float, size: float, fee_rate: float) -> float:
    """SHORT-only net PnL estimate for logging; the worker owns real accounting."""
    qty = size / entry
    gross = qty * (entry - exit_price)
    fee_in = fee_rate * size
    fee_out = fee_rate * (qty * exit_price)
    return gross - fee_in - fee_out


def _parse_json_rows(raw_rows: list) -> list[dict]:
    rows = []
    for raw in raw_rows or []:
        try:
            rows.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return rows


class ShortBtcV1Engine(BaseEngine):
    def __init__(self) -> None:
        super().__init__(settings)
        self._open_positions: dict[str, dict[str, Any]] = {}
        self._columns_config_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")
        self._context_redis: redis_lib.Redis | None = None

    # ── BaseEngine interface ──────────────────────────────────────────────

    def get_required_channels(self) -> list[str]:
        return [f"kline:{settings.TF}", f"kline:{settings.HTF}"]

    def _get_warmup_symbols(self) -> list[str]:
        return [] if self._is_blacklisted(settings.SYMBOL) else [settings.SYMBOL]

    def _has_open_positions(self) -> bool:
        return bool(self._open_positions)

    async def on_warmup_complete(self) -> None:
        if self._columns_config_path:
            self.load_columns_config(self._columns_config_path)

    async def scan_loop(self) -> None:
        """Abstract method required by BaseEngine; only exercised when this
        alpha runs standalone (outside the runner's per-symbol scan path)."""
        if not self._has_open_positions():
            return
        await self._manage_positions()

    # ── Per-symbol row for the runner's parallel scan path ────────────────

    def _build_symbol_row(self, symbol: str) -> dict[str, Any] | None:
        tf_map = self.symbol_data.get(symbol)
        if not tf_map:
            return None
        sd = tf_map.get(settings.TF)
        if not sd or not sd.price_list or not sd.high_list or not sd.low_list or not sd.open_list:
            return None
        d1_sd = tf_map.get(settings.HTF)
        return {
            "symbol": symbol,
            "close_list": sd.price_list,
            "high_list": sd.high_list,
            "low_list": sd.low_list,
            "open_list": sd.open_list,
            "time_list": sd.time_list,
            "signal_open_time_ms": sd.time_list[-1] if sd.time_list else 0,
            "d1_close_list": d1_sd.price_list if d1_sd else [],
            "d1_time_list": d1_sd.time_list if d1_sd else [],
        }

    def _tf_minutes(self) -> int:
        return self._tf_to_ms(settings.TF) // 60_000

    def _mds_context_redis(self) -> redis_lib.Redis:
        if self._context_redis is None:
            self._context_redis = redis_lib.from_url(
                self._mds_url, decode_responses=True, socket_timeout=5.0,
            )
        return self._context_redis

    def _fetch_context_rows(self, symbol: str) -> tuple[list[dict], list[dict]]:
        exchange = self._mds_exchange() or settings.EXCHANGE
        try:
            client = self._mds_context_redis()
            funding_raw = client.lrange(f"funding_snapshot:{exchange}:{symbol}", 0, -1)
            oi_raw = client.lrange(f"oi_snapshot:{exchange}:{symbol}:1d", 0, -1)
        except Exception as exc:
            logger.warning("[%s] MDS context fetch failed: %s", settings.ALPHA_ID, exc)
            self._context_redis = None
            return [], []
        funding_rows = _parse_json_rows(funding_raw)
        oi_rows = _parse_json_rows(oi_raw)
        funding_rows.sort(key=lambda r: r.get("funding_time", 0))
        oi_rows.sort(key=lambda r: r.get("open_time", 0))
        return funding_rows, oi_rows

    def _compute_indicators(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Runs on a worker thread (asyncio.to_thread) — the only place this
        engine performs blocking I/O (MDS Redis reads for funding/OI)."""
        symbol = row["symbol"]
        close_list = row["close_list"]
        high_list = row["high_list"]
        low_list = row["low_list"]
        open_list = row["open_list"]
        signal_open_time_ms = row["signal_open_time_ms"]

        indic: dict[str, Any] = {
            "signal_open_time_ms": signal_open_time_ms,
            "latest_close": close_list[-1],
            "latest_high": high_list[-1],
            "latest_low": low_list[-1],
            "entry_signal": None,
            "reduce_decision": None,
        }

        # Reading self._open_positions here (off the event-loop thread) is safe:
        # this is a single-symbol alpha, so _apply_decision for this symbol never
        # runs concurrently with this call, and dict.get() is a single atomic
        # bytecode op under the GIL.
        pos = self._open_positions.get(symbol)

        if pos is None:
            ema_fast = calc_ema(close_list, settings.EMA_FAST)
            ema_slow = calc_ema(close_list, settings.EMA_SLOW)
            rsi = calc_rsi(close_list, settings.RSI_LEN)
            atr = calc_atr(high_list, low_list, close_list, settings.ATR_LEN)

            tf_minutes = self._tf_minutes()
            lookback_bars = max(1, (settings.D1_GATE_LOOKBACK_HOURS * 60) // tf_minutes)

            entry_signal = compute_entry_signal(
                close_list, high_list, low_list, open_list,
                ema_fast, ema_slow, rsi, atr,
                lookback_bars=lookback_bars,
                rsi_thresh=settings.RSI_THRESH,
                clv_max=settings.CLV_MAX,
                sl_atr_mult=settings.SL_ATR_MULT,
                tp_ratio=settings.TP_RATIO,
            )

            if entry_signal is not None:
                candle_close_ms = signal_open_time_ms + _candle_seconds(settings.TF) * 1000
                d1_ema_fast = calc_ema(row["d1_close_list"], settings.D1_EMA_FAST)
                d1_ema_slow = calc_ema(row["d1_close_list"], settings.D1_EMA_SLOW)
                d1_ok = passes_d1_downtrend(
                    row["d1_close_list"], d1_ema_fast, d1_ema_slow, row["d1_time_list"],
                    signal_time_ms=candle_close_ms,
                    slope_lookback=settings.D1_SLOPE_LOOKBACK,
                )
                if d1_ok:
                    indic["entry_signal"] = entry_signal
        else:
            # Reduce evaluation fires once, on the first management bar after
            # entry, only if price moved against the short (mirrors backtest_v2's
            # `first_m15.close >= signal_close` gate) — this also avoids an MDS
            # Redis round-trip on every bar when the position is winning.
            price_rose = close_list[-1] >= pos.get("signal_close", pos["entry"])
            if not pos.get("reduced") and pos.get("bars_since_entry", 0) == 0 and price_rose:
                entry_time_ms = pos["entry_candle_open_ms"]
                funding_rows, oi_rows = self._fetch_context_rows(symbol)
                funding_row = read_last_at_or_before(funding_rows, "funding_time", entry_time_ms)
                oi_row, oi_prev_row = read_last_completed_daily(oi_rows, "open_time", entry_time_ms)
                funding_rate = funding_row.get("funding_rate") if funding_row else None
                oi_close = oi_row.get("oi_close") if oi_row else None
                oi_prev_close = oi_prev_row.get("oi_close") if oi_prev_row else None
                reduce_fraction, context_fields = compute_context_exit_fraction(
                    funding_rate, oi_close, oi_prev_close,
                )
                indic["reduce_decision"] = {
                    "reduce_fraction": reduce_fraction,
                    "context_fields": context_fields,
                }

        return indic

    # ── Decision application (event loop — no I/O) ────────────────────────

    def _apply_decision(self, row: dict[str, Any], indic: dict[str, Any]) -> None:
        symbol = row["symbol"]
        signal_open_time_ms = int(indic["signal_open_time_ms"])
        latest_close = float(indic["latest_close"])
        latest_high = float(indic["latest_high"])
        latest_low = float(indic["latest_low"])

        pos = self._open_positions.get(symbol)
        if pos is not None:
            if not self._claim_position_candle(pos, signal_open_time_ms):
                return

            reduce_decision = indic.get("reduce_decision")
            if reduce_decision and not pos.get("reduced"):
                self._reduce_position(symbol, pos, reduce_decision, latest_close)

            pos = self._open_positions.get(symbol)
            if pos is not None:
                self._manage_position(symbol, pos, latest_high, latest_low, latest_close, signal_open_time_ms)
                pos = self._open_positions.get(symbol)
                if pos is not None:
                    pos["bars_since_entry"] = pos.get("bars_since_entry", 0) + 1
            return

        if not self.can_open_new_trades() or len(self._open_positions) >= settings.MAX_CONCURRENT_POSITIONS:
            return

        entry_signal = indic.get("entry_signal")
        if entry_signal:
            self._open_position(symbol, entry_signal, signal_open_time_ms)

    # ── Position management ────────────────────────────────────────────────

    def _manage_position(
        self,
        symbol: str,
        pos: dict[str, Any],
        bar_high: float,
        bar_low: float,
        bar_close: float,
        signal_open_time_ms: int,
    ) -> None:
        sl = pos["sl"]
        tp = pos["tp"]

        now_ms = int(time.time() * 1000)
        hold_h = (now_ms - pos["opened_at_ms"]) / 3_600_000.0
        if hold_h >= settings.MAX_HOLD_H:
            self._close_position(symbol, pos, bar_close, "TIME", f"hold_h={hold_h:.2f}>=max={settings.MAX_HOLD_H}")
            return

        if bar_high >= sl:
            self._close_position(symbol, pos, sl, "SL", f"high={bar_high:.6f}>=sl={sl:.6f}")
            return

        if bar_low <= tp:
            self._close_position(symbol, pos, tp, "TP", f"low={bar_low:.6f}<=tp={tp:.6f}")
            return

    def _reduce_position(
        self, symbol: str, pos: dict[str, Any], reduce_decision: dict[str, Any], exit_price: float,
    ) -> None:
        reduce_fraction = float(reduce_decision["reduce_fraction"])
        context_fields = reduce_decision["context_fields"]
        reason = f"REDUCE{int(round(reduce_fraction * 100))}"
        metadata = {**context_fields, "reduce_fraction": reduce_fraction}

        if reduce_fraction >= 1.0:
            self.push_signal(
                "CLOSE", position_id=pos["position_id"], exit_price=exit_price,
                reason=reason, metadata=json.dumps(metadata),
            )
            self._open_positions.pop(symbol, None)
            self.mark_positions_changed()
            logger.info("[REDUCE-FULL] %s reason=%s @ %.6f frac=%.2f", symbol, reason, exit_price, reduce_fraction)
            return

        reduce_qty = pos["qty"] * reduce_fraction
        self.push_signal(
            "CLOSE", position_id=pos["position_id"], qty=reduce_qty, exit_price=exit_price,
            reason=reason, metadata=json.dumps(metadata),
        )
        pos["reduced"] = True
        pos["qty"] = pos["qty"] - reduce_qty
        self.mark_positions_changed()
        logger.info(
            "[REDUCE] %s reason=%s @ %.6f frac=%.2f remaining_qty=%.6f",
            symbol, reason, exit_price, reduce_fraction, pos["qty"],
        )

    def _open_position(self, symbol: str, entry_signal: dict[str, Any], signal_open_time_ms: int) -> None:
        if not self.can_open_new_trades():
            return
        if len(self._open_positions) >= settings.MAX_CONCURRENT_POSITIONS:
            return

        entry = entry_signal["entry"]
        sl = entry_signal["sl"]
        tp = entry_signal["tp"]
        notional = float(settings.INVEST_PER_TRADE) * int(settings.LEVERAGE)
        qty = notional / entry
        position_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        candle_close_ms = signal_open_time_ms + _candle_seconds(settings.TF) * 1000

        self._open_positions[symbol] = {
            "position_id": position_id,
            "side": "SHORT",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "qty": qty,
            "size": notional,
            "signal_close": entry_signal["signal_close"],
            "reduced": False,
            "bars_since_entry": 0,
            "entry_candle_open_ms": signal_open_time_ms,
            "signal_candle_close_ms": candle_close_ms,
            "opened_at_ms": int(time.time() * 1000),
        }
        self.mark_positions_changed()

        self.push_signal(
            "OPEN",
            symbol=symbol,
            side="SHORT",
            entry=entry,
            qty=qty,
            tp=tp,
            sl=sl,
            leverage=settings.LEVERAGE,
            position_id=position_id,
            exchange=settings.EXCHANGE,
            fee_pct=settings.FEE_PCT,
            metadata=json.dumps({
                "rule": "ema_downtrend_rsi_clv_breakdown_d1_gate",
                "sl_atr_mult": settings.SL_ATR_MULT,
                "tp_ratio": settings.TP_RATIO,
                "trade_size": round(notional, 2),
                "leverage": settings.LEVERAGE,
                "margin": round(notional / settings.LEVERAGE, 2),
            }),
            timestamp=timestamp,
        )
        logger.info("[OPEN] SHORT %s @ %.6f sl=%.6f tp=%.6f qty=%.6f", symbol, entry, sl, tp, qty)

    def _close_position(self, symbol: str, pos: dict[str, Any], exit_price: float, reason: str, detail: str = "") -> None:
        self.push_signal(
            "CLOSE",
            position_id=pos["position_id"],
            exit_price=exit_price,
            reason=reason,
            metadata=json.dumps({"close_detail": detail, "reduced": pos.get("reduced", False)}),
        )
        self._open_positions.pop(symbol, None)
        self.mark_positions_changed()

        net = _calc_net_pnl(pos["entry"], exit_price, pos["qty"] * pos["entry"], settings.FEE_PCT)
        logger.info(
            "[CLOSE] %s reason=%s @ %.6f net~=%.2f%s",
            symbol, reason, exit_price, net, f" | {detail}" if detail else "",
        )

    # ── Real-time tick-level SL/TP (price_alert) ───────────────────────────

    def on_price_alert_message(self, msg: dict[str, Any]) -> None:
        symbol = msg.get("symbol", "")
        if not symbol:
            return
        pos = self._open_positions.get(symbol)
        if not pos:
            return
        price = self._trigger_price("SHORT", msg)
        if price is None or price <= 0:
            return
        sl = pos["sl"]
        tp = pos["tp"]
        if price >= sl:
            self._close_position(symbol, pos, price, "SL", f"tick={price:.6f}>=sl={sl:.6f}")
        elif price <= tp:
            self._close_position(symbol, pos, price, "TP", f"tick={price:.6f}<=tp={tp:.6f}")

    # ── Periodic management (used by standalone scan_loop + the runner) ────

    async def _manage_positions(self) -> None:
        if not self._open_positions:
            return
        for symbol in list(self._open_positions.keys()):
            tf_map = self.symbol_data.get(symbol)
            if not tf_map:
                continue
            sd = tf_map.get(settings.TF)
            if not sd or not sd.price_list:
                continue
            pos = self._open_positions.get(symbol)
            if not pos:
                continue
            self._manage_position(
                symbol, pos,
                bar_high=sd.high_list[-1],
                bar_low=sd.low_list[-1],
                bar_close=sd.price_list[-1],
                signal_open_time_ms=sd.time_list[-1] if sd.time_list else 0,
            )
