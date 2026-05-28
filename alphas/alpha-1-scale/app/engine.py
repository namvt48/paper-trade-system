import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import compute_alpha_v5_indicators, reconstruct_trend_state
from base.engine import BaseEngine
from base.symbol_utils import get_binance_perp_symbols

logger = logging.getLogger(__name__)


def _candle_seconds(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    return 3600


def _calc_net_pnl(side: str, entry: float, exit_price: float, size: float, fee_rate: float) -> float:
    """Replicate backtest_v5 calc_pnl — returns net PnL after fees."""
    qty = size / entry
    gross = qty * (exit_price - entry) if side == "LONG" else qty * (entry - exit_price)
    fee_in = fee_rate * size
    fee_out = fee_rate * (qty * exit_price)
    return gross - fee_in - fee_out


class Alpha1ScaleEngine(BaseEngine):
    """Paper-trade engine implementing Alpha-1 with dynamic/scaled position sizing.

    Signal logic is identical to Alpha1FixedEngine (mirrors backtest_v5.py).
    Position sizing mirrors the backtest_v5 scaling formula:
        cur_size += SCALE_FACTOR * net_pnl          # grow on wins, shrink on losses
        cur_size  = clamp(cur_size,
                          MIN_INVEST,
                          SCALE_FACTOR * cur_equity) # cap at 30% of running equity

    _cur_size and _cur_eq are shared across all symbols (one pool per instance).
    They are in-memory only; a restart resets them to the configured initial values.

    Trend state is also in-memory; a restart resets to None per symbol.
    """

    def __init__(self):
        super().__init__(settings)
        # position_id, side, entry, sl, tp, trail_distance, hse, lse, size
        self._open_positions: dict[str, dict] = {}
        # Per-symbol asymmetric trend state (None | True=bull | False=bear)
        self._trend_state: dict[str, Optional[bool]] = {}

        # Shared sizing state — one pool for the whole instance
        self._cur_eq: float = settings.CAPITAL
        self._cur_size: float = settings.INVEST_PER_TRADE

        # {symbol → max_leverage} loaded from LEVERAGE_FILE; fallback = settings.LEVERAGE
        self._leverage_map: dict[str, int] = {}
        self._load_leverage_file()
        self._load_blacklist_file()

    # ── Leverage file loading ─────────────────────────────────────────────────

    def _load_leverage_file(self) -> None:
        path = settings.LEVERAGE_FILE
        if not path or not os.path.isfile(path):
            logger.warning("[%s] LEVERAGE_FILE not found: %s — using default %dx", settings.ALPHA_ID, path, settings.LEVERAGE)
            return
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
        self._leverage_map = {r["symbol"]: int(r["max_leverage"]) for r in rows if "symbol" in r and "max_leverage" in r}
        logger.info("[%s] Leverage map loaded: %d symbols from %s", settings.ALPHA_ID, len(self._leverage_map), path)

    def _get_symbol_leverage(self, symbol: str) -> int:
        return self._leverage_map.get(symbol, settings.LEVERAGE)

    # ── Blacklist file loading ────────────────────────────────────────────────

    def _load_blacklist_file(self) -> None:
        path = settings.BLACKLIST_FILE
        if not path:
            return
        if not os.path.isfile(path):
            logger.warning("[%s] BLACKLIST_FILE not found: %s", settings.ALPHA_ID, path)
            return
        loaded: set[str] = set()
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                loaded.add(line.upper())
        self._blacklist |= loaded
        logger.info(
            "[%s] Blacklist file loaded: %d symbols from %s (total blacklisted: %d)",
            settings.ALPHA_ID, len(loaded), path, len(self._blacklist),
        )

    # ── BaseEngine interface ──────────────────────────────────────────────────

    def get_required_channels(self) -> list[str]:
        return [f"kline:{settings.TF}"]

    def _get_warmup_symbols(self) -> list[str]:
        return [s for s in get_binance_perp_symbols() if not self._is_blacklisted(s)]

    async def on_warmup_complete(self) -> None:
        count = 0
        for symbol, tf_map in self.symbol_data.items():
            sd = tf_map.get(settings.TF)
            if not sd or not sd.price_list:
                continue
            trend = reconstruct_trend_state(
                list(sd.price_list),
                settings.SMA_LEN,
                settings.NORM_WINDOW,
                settings.THRESHOLD,
            )
            if trend is not None:
                self._trend_state[symbol] = trend
                count += 1
        logger.info("[%s] Trend state reconstructed for %d/%d symbols", settings.ALPHA_ID, count, len(self.symbol_data))

    async def scan_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self._wait_until_next_candle_offset()
                if self.shutdown_event.is_set():
                    break
                await self._process_all_symbols()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scan error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _wait_until_next_candle_offset(self) -> None:
        candle_len = _candle_seconds(settings.TF)
        now = time.time()
        next_candle = (int(now // candle_len) + 1) * candle_len
        target = next_candle + settings.OFFSET_CANDLE_SEC
        wait = target - now
        if wait > 0:
            await asyncio.sleep(wait)

    async def _process_all_symbols(self) -> None:
        snapshot: list[dict] = []
        async with self.data_lock:
            for symbol, tf_map in self.symbol_data.items():
                sd = tf_map.get(settings.TF)
                if not sd or not sd.price_list or not sd.high_list or not sd.low_list:
                    continue
                snapshot.append({
                    "symbol": symbol,
                    "close_list": list(sd.price_list),
                    "high_list": list(sd.high_list),
                    "low_list": list(sd.low_list),
                })

        for row in snapshot:
            self._process_symbol(row)

    def _process_symbol(self, row: dict) -> None:
        symbol = row["symbol"]
        indic = compute_alpha_v5_indicators(
            close_list=row["close_list"],
            high_list=row["high_list"],
            low_list=row["low_list"],
        )
        if indic is None:
            return

        acol: float = indic["acol"]
        acol_prev: float = indic["acol_prev"]
        atr: float = indic["atr"]
        poc: float = indic["poc"]
        close: float = indic["close"]
        high: float = indic["high"]
        low: float = indic["low"]

        threshold = settings.THRESHOLD
        poc_filter = settings.POC_FILTER_PCT

        # ── Update trend state (asymmetric, same logic as backtest_v5) ────────
        old_trend = self._trend_state.get(symbol)
        trend = old_trend
        if acol_prev <= threshold and acol > threshold and old_trend is not True:
            trend = True
        if acol_prev >= -threshold and acol < -threshold and old_trend is True:
            trend = False
        trend_changed = trend != old_trend
        self._trend_state[symbol] = trend

        pos = self._open_positions.get(symbol)

        if pos is not None:
            if not trend_changed:
                self._manage_existing_position(symbol, pos, acol, poc, close, high, low, threshold, poc_filter)
            elif trend is not None:
                gb = close > poc * (1 + poc_filter)
                gbe = close < poc * (1 - poc_filter)
                ce = (trend and gb) or (not trend and gbe)
                if ce:
                    self._close_position(symbol, pos, close, "REV")
                    self._open_new_position(symbol, trend, close, high, low, atr, poc)
        else:
            if trend_changed and trend is not None:
                gb = close > poc * (1 + poc_filter)
                gbe = close < poc * (1 - poc_filter)
                ce = (trend and gb) or (not trend and gbe)
                if ce and len(self._open_positions) < settings.MAX_CONCURRENT_POSITIONS:
                    self._open_new_position(symbol, trend, close, high, low, atr, poc)

    def _manage_existing_position(
        self,
        symbol: str,
        pos: dict,
        acol: float,
        poc: float,
        close: float,
        high: float,
        low: float,
        threshold: float,
        poc_filter: float,
    ) -> None:
        side = pos["side"]

        # 1. CUT — counter-trend signal fires before SL/TP check
        cut = False
        if side == "LONG" and (acol < -threshold or close < poc * (1 - poc_filter)):
            cut = True
        elif side == "SHORT" and (acol > threshold or close > poc * (1 + poc_filter)):
            cut = True
        if cut:
            self._close_position(symbol, pos, close, "CUT")
            return

        trail_dist = pos["trail_distance"]

        # 2. Check SL/TP with the SL from the PREVIOUS bar (before updating trailing).
        #    This mirrors backtest_v5: trailing is updated AFTER the hit check each bar.
        if side == "LONG":
            if low <= pos["sl"]:
                self._close_position(symbol, pos, pos["sl"], "SL")
                return
            if high >= pos["tp"]:
                self._close_position(symbol, pos, pos["tp"], "TP")
                return
            # 3. Raise trailing SL for the NEXT bar
            pos["hse"] = max(pos["hse"], high)
            new_sl = pos["hse"] - trail_dist
            if new_sl > pos["sl"]:
                pos["sl"] = new_sl
                self.push_signal("MODIFY", position_id=pos["position_id"], sl=new_sl)
                logger.debug("[MODIFY] %s LONG new_sl=%.6f", symbol, new_sl)
        else:
            if high >= pos["sl"]:
                self._close_position(symbol, pos, pos["sl"], "SL")
                return
            if low <= pos["tp"]:
                self._close_position(symbol, pos, pos["tp"], "TP")
                return
            # 3. Lower trailing SL for the NEXT bar
            pos["lse"] = min(pos["lse"], low)
            new_sl = pos["lse"] + trail_dist
            if new_sl < pos["sl"]:
                pos["sl"] = new_sl
                self.push_signal("MODIFY", position_id=pos["position_id"], sl=new_sl)
                logger.debug("[MODIFY] %s SHORT new_sl=%.6f", symbol, new_sl)

    def _open_new_position(
        self,
        symbol: str,
        trend: bool,
        close: float,
        high: float,
        low: float,
        atr: float,
        poc: float,
    ) -> None:
        side = "LONG" if trend else "SHORT"
        trail_dist = settings.TRAIL_ATR_MULT * atr
        tp_dist = settings.TRAIL_ATR_MULT * settings.TP_RATIO * atr
        entry = close

        if side == "LONG":
            sl = entry - trail_dist
            tp = entry + tp_dist
        else:
            sl = entry + trail_dist
            tp = entry - tp_dist

        # Snapshot cur_size at entry (notional) — same as backtest_v5 `trade_size = cur_size`
        trade_size = self._cur_size
        symbol_lev = self._get_symbol_leverage(symbol)
        # notional = trade_size (cur_size); margin = trade_size / symbol_lev
        qty = trade_size / entry
        position_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()

        self._open_positions[symbol] = {
            "position_id": position_id,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "trail_distance": trail_dist,
            "hse": high,
            "lse": low,
            "size": trade_size,     # notional — used by _close_position to compute PnL
        }

        self.push_signal(
            "OPEN",
            symbol=symbol,
            side=side,
            entry=entry,
            qty=qty,
            tp=tp,
            sl=sl,
            leverage=symbol_lev,
            position_id=position_id,
            exchange=settings.EXCHANGE,
            fee_pct=settings.FEE_PCT,
            metadata=json.dumps({
                "atr": round(atr, 6),
                "poc": round(poc, 6),
                "trail_distance": round(trail_dist, 6),
                "trend": "BULL" if trend else "BEAR",
                "trade_size": round(trade_size, 2),
                "leverage": symbol_lev,
                "margin": round(trade_size / symbol_lev, 2),
                "cur_equity": round(self._cur_eq, 2),
            }),
            timestamp=timestamp,
        )

        logger.info(
            "[OPEN] %s %s @ %.4f sl=%.4f tp=%.4f size=%.2f lev=%dx margin=%.2f equity=%.2f",
            side, symbol, entry, sl, tp, trade_size, symbol_lev, trade_size / symbol_lev, self._cur_eq,
        )

    def _close_position(self, symbol: str, pos: dict, exit_price: float, reason: str) -> None:
        self.push_signal(
            "CLOSE",
            position_id=pos["position_id"],
            exit_price=exit_price,
            reason=reason,
        )
        self._open_positions.pop(symbol, None)

        # ── Update dynamic sizing state (mirrors backtest_v5 close_trade) ────
        trade_size = pos.get("size", self._cur_size)
        net = _calc_net_pnl(pos["side"], pos["entry"], exit_price, trade_size, settings.FEE_PCT)

        self._cur_eq += net
        self._cur_size += settings.SCALE_FACTOR * net
        max_sz = settings.SCALE_FACTOR * self._cur_eq
        self._cur_size = max(settings.MIN_INVEST, min(self._cur_size, max_sz))

        logger.info(
            "[CLOSE] %s reason=%s @ %.6f  net=%.2f  next_size=%.2f  equity=%.2f",
            symbol, reason, exit_price, net, self._cur_size, self._cur_eq,
        )
