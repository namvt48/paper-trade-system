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
from app.strategy import compute_alpha_v5b_indicators, reconstruct_trend_state
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
    qty = size / entry
    gross = qty * (exit_price - entry) if side == "LONG" else qty * (entry - exit_price)
    fee_in = fee_rate * size
    fee_out = fee_rate * (qty * exit_price)
    return gross - fee_in - fee_out


class Alpha1V5bEngine(BaseEngine):
    """Paper-trade engine for Alpha-1 V5b.

    Improvements over the base Alpha-1 model:
    - Adaptive ATR trailing stop (TRAIL_ATR_MIN–MAX range driven by trend strength)
    - Kelly-inspired sizing (rolling KELLY_LOOKBACK-trade win-rate multiplier)
    - Time-based exit: force-close after MAX_TRADE_BARS candles (TIME reason)
    - Min hold guard: skip SL/TP/CUT for first MIN_HOLD_BARS after entry
    - TP_RATIO = 2.0 (down from 3.0, empirically better on M15)
    - Continuous trailing SL (not just BE snap — mirrors backtest exactly)

    Sizing pool is shared across all symbols (one pool per instance).
    Trend state and positions are in-memory; a restart resets them.
    """

    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}
        self._trend_state: dict[str, Optional[bool]] = {}

        # Shared sizing pool
        self._cur_eq: float = settings.CAPITAL
        self._cur_size: float = settings.INVEST_PER_TRADE

        # Rolling trade results for Kelly multiplier (True = win, False = loss)
        self._recent_results: list[bool] = []

        self._leverage_map: dict[str, int] = {}
        self._load_leverage_file()
        self._load_blacklist_file()
        self._columns_config_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")

    # ── Leverage / blacklist file loading ────────────────────────────────────

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

    def _has_open_positions(self) -> bool:
        return len(self._open_positions) > 0

    async def _manage_positions(self) -> None:
        if not self._open_positions:
            return

        # Collect unprocessed 1m candles per symbol since last manage run.
        # Using min-low / max-high across all unprocessed candles avoids missing
        # SL/TP hits that occur and recover between manage_loop ticks (Bug 1 fix).
        snapshots = {}
        async with self.data_lock:
            for symbol, pos in list(self._open_positions.items()):
                sd = self.symbol_data.get(symbol, {}).get("1m")
                if not (sd and sd.price_list and sd.high_list and sd.low_list and sd.time_list):
                    continue
                threshold_ms = pos.get("signal_candle_close_ms", 0)
                last_ms = pos.get("last_managed_ms", threshold_ms)
                # Find all candles that closed after last check
                indices = [i for i, t in enumerate(sd.time_list) if t > last_ms]
                if not indices:
                    continue
                worst_high = max(sd.high_list[i] for i in indices)
                worst_low  = min(sd.low_list[i]  for i in indices)
                latest_ms  = sd.time_list[indices[-1]]
                snapshots[symbol] = {
                    "close":           sd.price_list[indices[-1]],
                    "high":            worst_high,
                    "low":             worst_low,
                    "current_time_ms": latest_ms,
                }
                pos["last_managed_ms"] = latest_ms

        for symbol, snap in snapshots.items():
            if symbol not in self._open_positions:
                continue
            self._manage_existing_position(
                symbol,
                self._open_positions[symbol],
                close=snap["close"],
                high=snap["high"],
                low=snap["low"],
                current_time_ms=snap["current_time_ms"],
            )

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
                    "symbol":             symbol,
                    "close_list":         sd.price_list,
                    "high_list":          sd.high_list,
                    "low_list":           sd.low_list,
                    "signal_open_time_ms": sd.time_list[-1] if sd.time_list else 0,
                })

        for row in snapshot:
            self._process_symbol(row)

    def _adaptive_trail(self, acol: float, atr: float) -> float:
        """Compute adaptive trail distance.

        Stronger trend (|acol| → 1) → TRAIL_ATR_MIN (tighter, follow the move).
        Weaker trend  (|acol| → 0) → TRAIL_ATR_MAX (wider, tolerate noise).
        """
        strength = min(1.0, max(0.0, abs(acol)))
        mult = settings.TRAIL_ATR_MAX - strength * (settings.TRAIL_ATR_MAX - settings.TRAIL_ATR_MIN)
        return mult * atr

    def _kelly_size(self) -> float:
        """Return Kelly-adjusted trade size based on rolling win rate."""
        if len(self._recent_results) < 5:
            return self._cur_size
        recent = self._recent_results[-settings.KELLY_LOOKBACK:]
        wr = sum(recent) / len(recent)
        multiplier = min(2.0, max(0.5, wr / settings.KELLY_BASE_WR))
        return max(settings.MIN_INVEST, min(self._cur_size * multiplier, settings.SCALE_FACTOR * self._cur_eq))

    def _bars_held(self, pos: dict, current_candle_open_ms: int) -> int:
        candle_ms = _candle_seconds(settings.TF) * 1000
        entry_ms = pos.get("entry_candle_open_ms", current_candle_open_ms)
        return max(0, (current_candle_open_ms - entry_ms) // candle_ms)

    def _process_symbol(self, row: dict) -> None:
        symbol = row["symbol"]
        signal_open_time_ms: int = row.get("signal_open_time_ms", 0)

        indic = compute_alpha_v5b_indicators(
            close_list=row["close_list"],
            high_list=row["high_list"],
            low_list=row["low_list"],
        )
        if indic is None:
            return

        acol: float      = indic["acol"]
        acol_prev: float = indic["acol_prev"]
        atr: float       = indic["atr"]
        poc: float       = indic["poc"]
        close: float     = indic["close"]
        high: float      = indic["high"]
        low: float       = indic["low"]

        threshold  = settings.THRESHOLD
        poc_filter = settings.POC_FILTER_PCT

        # ── Trend state update ────────────────────────────────────────────────
        old_trend = self._trend_state.get(symbol)
        trend = old_trend
        if acol_prev <= threshold and acol > threshold and old_trend is not True:
            trend = True
        if acol_prev >= -threshold and acol < -threshold and old_trend is True:
            trend = False
        trend_changed = trend != old_trend
        self._trend_state[symbol] = trend

        pos  = self._open_positions.get(symbol)
        side = pos["side"] if pos else None

        if pos is not None:
            bars = self._bars_held(pos, signal_open_time_ms)

            # TIME exit — force close regardless of trend or hold period
            if bars >= settings.MAX_TRADE_BARS:
                self._close_position(symbol, pos, close, "TIME", f"bars_held={bars}")
                return

            # Update adaptive trail distance for 1m manage loop
            pos["trail_distance"] = self._adaptive_trail(acol, atr)

            if not trend_changed:
                cut = False
                if bars >= settings.MIN_HOLD_BARS:
                    if side == "LONG" and acol < -threshold:
                        self._close_position(symbol, pos, close, "CUT", f"acol={acol:.3f} < -{threshold}")
                        cut = True
                    elif side == "LONG" and close < poc * (1 - poc_filter):
                        self._close_position(symbol, pos, close, "CUT", f"close={close:.6f} < poc*(1-{poc_filter})")
                        cut = True
                    elif side == "SHORT" and acol > threshold:
                        self._close_position(symbol, pos, close, "CUT", f"acol={acol:.3f} > +{threshold}")
                        cut = True
                    elif side == "SHORT" and close > poc * (1 + poc_filter):
                        self._close_position(symbol, pos, close, "CUT", f"close={close:.6f} > poc*(1+{poc_filter})")
                        cut = True
                if not cut:
                    self._manage_existing_position(symbol, pos, close, high, low, signal_open_time_ms)

            elif trend is not None:
                # REV: trend flip while in position — min hold does NOT apply to REV
                gb  = close > poc * (1 + poc_filter)
                gbe = close < poc * (1 - poc_filter)
                ce  = (trend and gb) or (not trend and gbe)
                if ce:
                    rev_detail = f"acol={acol_prev:.3f}→{acol:.3f} trend→{'BULL' if trend else 'BEAR'} | close/poc={close/poc:.4f}"
                    self._close_position(symbol, pos, close, "REV", rev_detail)
                    self._open_new_position(symbol, trend, close, high, low, atr, poc, acol, acol_prev, signal_open_time_ms)

        else:
            if trend_changed and trend is not None:
                gb  = close > poc * (1 + poc_filter)
                gbe = close < poc * (1 - poc_filter)
                ce  = (trend and gb) or (not trend and gbe)
                if ce and len(self._open_positions) < settings.MAX_CONCURRENT_POSITIONS:
                    self._open_new_position(symbol, trend, close, high, low, atr, poc, acol, acol_prev, signal_open_time_ms)

    def _manage_existing_position(
        self,
        symbol: str,
        pos: dict,
        close: float,
        high: float,
        low: float,
        current_time_ms: int = 0,
    ) -> None:
        """Check SL/TP and update trailing SL.

        Skipped entirely within the MIN_HOLD_BARS window to avoid whipsaw exits.
        Trailing SL is continuous (mirrors backtest), not just BE snap.
        Candle-based fills: SL uses candle extreme (conservative), TP uses target level.
        """
        # Min hold guard
        if current_time_ms and self._bars_held(pos, current_time_ms) < settings.MIN_HOLD_BARS:
            return

        side       = pos["side"]
        trail_dist = pos["trail_distance"]

        if side == "LONG":
            # Trail SL up as new highs are made
            pos["hse"] = max(pos["hse"], high)
            new_sl = pos["hse"] - trail_dist
            if new_sl > pos["sl"]:
                old_sl = pos["sl"]
                pos["sl"] = new_sl
                self.push_signal("MODIFY", position_id=pos["position_id"], sl=new_sl)
                logger.debug("[MODIFY] %s LONG trail sl %.6f→%.6f | hse=%.6f", symbol, old_sl, new_sl, pos["hse"])
            if low <= pos["sl"]:
                self._close_position(
                    symbol, pos, low, "SL", f"low={low:.6f} <= sl={pos['sl']:.6f}",
                    metadata=self._build_candle_close_metadata(
                        reason="SL", stop_price=pos["sl"], trigger_price=low,
                        fill_price=low, candle_high=high, candle_low=low,
                    ),
                )
                return
            if high >= pos["tp"]:
                self._close_position(
                    symbol, pos, pos["tp"], "TP", f"high={high:.6f} >= tp={pos['tp']:.6f}",
                    metadata=self._build_candle_close_metadata(
                        reason="TP", stop_price=pos["tp"], trigger_price=high,
                        fill_price=pos["tp"], candle_high=high, candle_low=low,
                    ),
                )
        else:
            # Trail SL down as new lows are made
            pos["lse"] = min(pos["lse"], low)
            new_sl = pos["lse"] + trail_dist
            if new_sl < pos["sl"]:
                old_sl = pos["sl"]
                pos["sl"] = new_sl
                self.push_signal("MODIFY", position_id=pos["position_id"], sl=new_sl)
                logger.debug("[MODIFY] %s SHORT trail sl %.6f→%.6f | lse=%.6f", symbol, old_sl, new_sl, pos["lse"])
            if high >= pos["sl"]:
                self._close_position(
                    symbol, pos, high, "SL", f"high={high:.6f} >= sl={pos['sl']:.6f}",
                    metadata=self._build_candle_close_metadata(
                        reason="SL", stop_price=pos["sl"], trigger_price=high,
                        fill_price=high, candle_high=high, candle_low=low,
                    ),
                )
                return
            if low <= pos["tp"]:
                self._close_position(
                    symbol, pos, pos["tp"], "TP", f"low={low:.6f} <= tp={pos['tp']:.6f}",
                    metadata=self._build_candle_close_metadata(
                        reason="TP", stop_price=pos["tp"], trigger_price=low,
                        fill_price=pos["tp"], candle_high=high, candle_low=low,
                    ),
                )

    def on_price_alert_message(self, msg: dict) -> None:
        """Real-time SL/TP check on side-aware MDS price_alert ticks."""
        symbol = msg.get("symbol", "")
        if not symbol:
            return

        pos = self._open_positions.get(symbol)
        if not pos:
            return

        price = self._trigger_price(pos["side"], msg)
        if price is None:
            return

        # Min hold guard — same logic as _manage_existing_position
        import time as _time
        current_time_ms = int(_time.time() * 1000)
        if self._bars_held(pos, current_time_ms) < settings.MIN_HOLD_BARS:
            return

        side = pos["side"]

        if side == "LONG":
            # Update trailing high with real-time price
            if price > pos["hse"]:
                pos["hse"] = price
                new_sl = pos["hse"] - pos["trail_distance"]
                if new_sl > pos["sl"]:
                    pos["sl"] = new_sl
                    self.push_signal("MODIFY", position_id=pos["position_id"], sl=new_sl)
            if price <= pos["sl"]:
                self._close_position(
                    symbol, pos, price, "SL",
                    f"tick={price:.6f}<=sl={pos['sl']:.6f}",
                    metadata=self._build_close_metadata(reason="SL", stop_price=pos["sl"], trigger_price=price, tick=msg),
                )
            elif price >= pos["tp"]:
                self._close_position(
                    symbol, pos, price, "TP",
                    f"tick={price:.6f}>=tp={pos['tp']:.6f}",
                    metadata=self._build_close_metadata(reason="TP", stop_price=pos["tp"], trigger_price=price, tick=msg),
                )
        else:
            if price < pos["lse"]:
                pos["lse"] = price
                new_sl = pos["lse"] + pos["trail_distance"]
                if new_sl < pos["sl"]:
                    pos["sl"] = new_sl
                    self.push_signal("MODIFY", position_id=pos["position_id"], sl=new_sl)
            if price >= pos["sl"]:
                self._close_position(
                    symbol, pos, price, "SL",
                    f"tick={price:.6f}>=sl={pos['sl']:.6f}",
                    metadata=self._build_close_metadata(reason="SL", stop_price=pos["sl"], trigger_price=price, tick=msg),
                )
            elif price <= pos["tp"]:
                self._close_position(
                    symbol, pos, price, "TP",
                    f"tick={price:.6f}<=tp={pos['tp']:.6f}",
                    metadata=self._build_close_metadata(reason="TP", stop_price=pos["tp"], trigger_price=price, tick=msg),
                )

    def _open_new_position(
        self,
        symbol: str,
        trend: bool,
        close: float,
        high: float,
        low: float,
        atr: float,
        poc: float,
        acol: float,
        acol_prev: float,
        signal_open_time_ms: int = 0,
    ) -> None:
        if not self.can_open_new_trades():
            return
        side       = "LONG" if trend else "SHORT"
        trail_dist = self._adaptive_trail(acol, atr)
        tp_dist    = trail_dist * settings.TP_RATIO
        entry      = close

        if side == "LONG":
            sl = entry - trail_dist
            tp = entry + tp_dist
            hse, lse = entry, entry
        else:
            sl = entry + trail_dist
            tp = entry - tp_dist
            hse, lse = entry, entry

        trade_size   = self._kelly_size()
        symbol_lev   = self._get_symbol_leverage(symbol)
        qty          = trade_size / entry
        position_id  = str(uuid.uuid4())
        timestamp    = datetime.now(timezone.utc).isoformat()

        signal_candle_close_ms = signal_open_time_ms + _candle_seconds(settings.TF) * 1000
        self._open_positions[symbol] = {
            "position_id":          position_id,
            "side":                 side,
            "entry":                entry,
            "sl":                   sl,
            "tp":                   tp,
            "trail_distance":       trail_dist,
            "hse":                  hse,
            "lse":                  lse,
            "size":                 trade_size,
            "entry_candle_open_ms": signal_open_time_ms,
            "signal_candle_close_ms": signal_candle_close_ms,
        }
        self.mark_positions_changed()

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
                "atr":           round(atr, 6),
                "poc":           round(poc, 6),
                "trail_distance": round(trail_dist, 6),
                "trail_mult":    round(trail_dist / atr, 3),
                "trend":         "BULL" if trend else "BEAR",
                "trade_size":    round(trade_size, 2),
                "leverage":      symbol_lev,
                "margin":        round(trade_size / symbol_lev, 2),
                "cur_equity":    round(self._cur_eq, 2),
                "kelly_trades":  len(self._recent_results),
            }),
            timestamp=timestamp,
        )

        thr = settings.THRESHOLD
        logger.info(
            "[OPEN] %s %s @ %.6f sl=%.6f tp=%.6f trail=%.4f(%.3fx) size=%.2f lev=%dx equity=%.2f | acol=%.3f→%.3f x%+.3f poc=%.6f ratio=%.4f",
            side, symbol, entry, sl, tp, trail_dist, trail_dist / atr,
            trade_size, symbol_lev, self._cur_eq,
            acol_prev, acol, thr if trend else -thr,
            poc, close / poc,
        )

    def _close_position(self, symbol: str, pos: dict, exit_price: float, reason: str, detail: str = "", metadata: str | None = None) -> None:
        self.push_signal(
            "CLOSE",
            position_id=pos["position_id"],
            exit_price=exit_price,
            reason=reason,
            metadata=metadata,
        )
        self._open_positions.pop(symbol, None)
        self.mark_positions_changed()

        trade_size = pos.get("size", self._cur_size)
        net = _calc_net_pnl(pos["side"], pos["entry"], exit_price, trade_size, settings.FEE_PCT)

        # Track win/loss for Kelly
        self._recent_results.append(net > 0)
        if len(self._recent_results) > settings.KELLY_LOOKBACK * 2:
            self._recent_results = self._recent_results[-settings.KELLY_LOOKBACK * 2:]

        # Update shared sizing pool (mirrors backtest_v5 close_trade)
        self._cur_eq   += net
        self._cur_size += settings.SCALE_FACTOR * net
        max_sz          = settings.SCALE_FACTOR * self._cur_eq
        self._cur_size  = max(settings.MIN_INVEST, min(self._cur_size, max_sz))

        logger.info(
            "[CLOSE] %s reason=%s @ %.6f  net=%.2f  kelly_wr=%.0f%%(%d)  next_size=%.2f  equity=%.2f%s",
            symbol, reason, exit_price, net,
            (sum(self._recent_results[-settings.KELLY_LOOKBACK:]) / min(len(self._recent_results), settings.KELLY_LOOKBACK) * 100)
            if self._recent_results else 0,
            len(self._recent_results),
            self._cur_size, self._cur_eq,
            f" | {detail}" if detail else "",
        )
