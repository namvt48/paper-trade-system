import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import compute_greenred, compute_strategy, combined_state, attach_htf_bias, vol_target_scale
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

class Alpha2Engine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}
        self._cur_eq: float = settings.CAPITAL
        self._cur_size: float = settings.INVEST_PER_TRADE

        self._leverage_map: dict[str, int] = {}
        self._load_leverage_file()
        self._load_blacklist_file()

    def _load_leverage_file(self) -> None:
        path = settings.LEVERAGE_FILE
        if not path or not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
        self._leverage_map = {r["symbol"]: int(r["max_leverage"]) for r in rows if "symbol" in r and "max_leverage" in r}

    def _get_symbol_leverage(self, symbol: str) -> int:
        return self._leverage_map.get(symbol, settings.LEVERAGE)

    def _load_blacklist_file(self) -> None:
        path = settings.BLACKLIST_FILE
        if not path:
            return
        if not os.path.isfile(path):
            return
        loaded: set[str] = set()
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                loaded.add(line.upper())
        self._blacklist |= loaded

    def get_required_channels(self) -> list[str]:
        return [f"kline:{settings.TF}", f"kline:{settings.HTF}"]

    def _get_warmup_symbols(self) -> list[str]:
        return [s for s in get_binance_perp_symbols() if not self._is_blacklisted(s)]

    def _has_open_positions(self) -> bool:
        return len(self._open_positions) > 0

    async def _manage_positions(self) -> None:
        pass

    async def on_warmup_complete(self) -> None:
        logger.info("[%s] Warmup complete.", settings.ALPHA_ID)

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

    async def _wait_until_next_candle_offset(self) -> None:
        candle_len = _candle_seconds(settings.TF)
        now = time.time()
        next_candle = (int(now // candle_len) + 1) * candle_len
        target = next_candle + settings.OFFSET_CANDLE_SEC
        wait = target - now
        if wait > 0:
            await asyncio.sleep(wait)

    def _build_symbol_row(self, symbol: str) -> dict | None:
        """Build row dict for a single symbol (base TF + HTF data)."""
        tf_map = self.symbol_data.get(symbol)
        if not tf_map:
            return None
        sd_base = tf_map.get(settings.TF)
        sd_htf = tf_map.get(settings.HTF)
        if not sd_base or not sd_htf or not sd_base.price_list or not sd_htf.price_list:
            return None
        return {
            "symbol": symbol,
            "base_close_list": sd_base.price_list,
            "base_time_list": sd_base.time_list,
            "htf_close_list": sd_htf.price_list,
            "htf_time_list": sd_htf.time_list,
        }

    async def _process_all_symbols(self) -> None:
        snapshot: list[dict] = []
        async with self.data_lock:
            for symbol in self.symbol_data:
                row = self._build_symbol_row(symbol)
                if row:
                    snapshot.append(row)

        for row in snapshot:
            self._process_symbol(row)

    def _process_symbol(self, row: dict) -> None:
        """Backward-compat wrapper: compute indicators then apply decision."""
        indic = self._compute_indicators(row)
        if indic is not None:
            self._apply_decision(row, indic)

    def _compute_indicators(self, row: dict) -> dict | None:
        """Pure indicator computation — thread-safe, no side effects."""
        df_base = pd.DataFrame({
            "time": pd.to_datetime(row["base_time_list"], unit="ms", utc=True),
            "close": row["base_close_list"]
        })
        df_htf = pd.DataFrame({
            "time": pd.to_datetime(row["htf_time_list"], unit="ms", utc=True),
            "close": row["htf_close_list"]
        })

        if len(df_base) < settings.DENOM_LEN or len(df_htf) < 5:
            return None

        tc = np.array(compute_greenred(df_base))
        sc = np.array(compute_strategy(df_base))
        state = combined_state(tc, sc)
        long_sig = state == "long"
        short_sig = state == "short"
        size_arr = vol_target_scale(df_base)
        hbias = attach_htf_bias(df_base, df_htf, settings.HTF, settings.TF)

        return {
            "latest_long_sig": bool(long_sig[-1]),
            "latest_short_sig": bool(short_sig[-1]),
            "latest_hbias": hbias[-1],
            "latest_size": float(size_arr[-1]),
            "close": df_base["close"].iloc[-1],
            "signal_open_time_ms": int(df_base["time"].iloc[-1].timestamp() * 1000),
        }

    def _apply_decision(self, row: dict, indic: dict) -> None:
        """Apply trading decisions — mutates state, must run sequentially."""
        symbol = row["symbol"]
        latest_long_sig = indic["latest_long_sig"]
        latest_short_sig = indic["latest_short_sig"]
        latest_hbias = indic["latest_hbias"]
        latest_size = indic["latest_size"]
        close = indic["close"]
        signal_open_time_ms = indic["signal_open_time_ms"]

        pos = self._open_positions.get(symbol)
        current_pos = 1 if (pos and pos["side"] == "LONG") else -1 if (pos and pos["side"] == "SHORT") else 0

        allow_long = latest_hbias != "short"
        allow_short = latest_hbias != "long"

        action = "HOLD"
        side = "FLAT"
        if latest_long_sig and current_pos <= 0 and allow_long and latest_size > 0:
            action = "REVERSE->LONG" if current_pos < 0 else "OPEN_LONG"
            side = "LONG"
        elif latest_short_sig and current_pos >= 0 and allow_short and latest_size > 0:
            action = "REVERSE->SHORT" if current_pos > 0 else "OPEN_SHORT"
            side = "SHORT"

        if action in ("REVERSE->LONG", "REVERSE->SHORT", "OPEN_LONG", "OPEN_SHORT"):
            if pos:
                self._close_position(symbol, pos, close, "REVERSE", f"action={action}")
            if len(self._open_positions) < settings.MAX_CONCURRENT_POSITIONS:
                self._open_new_position(symbol, side, close, latest_size, latest_hbias, signal_open_time_ms)

    def _open_new_position(self, symbol: str, side: str, close: float, latest_size: float, latest_hbias: str, signal_open_time_ms: int) -> None:
        if not self.can_open_new_trades():
            return
        
        trade_size = max(settings.MIN_INVEST, min(self._cur_size * latest_size, settings.CAPITAL))
        symbol_lev = self._get_symbol_leverage(symbol)
        qty = trade_size / close
        position_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        
        sl = close * 0.1 if side == "LONG" else close * 3.0
        tp = close * 10.0 if side == "LONG" else close * 0.1

        signal_candle_close_ms = signal_open_time_ms + _candle_seconds(settings.TF) * 1000
        self._open_positions[symbol] = {
            "position_id": position_id,
            "side": side,
            "entry": close,
            "sl": sl,
            "tp": tp,
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
            tp=tp,
            sl=sl,
            leverage=symbol_lev,
            position_id=position_id,
            exchange=getattr(settings, "EXCHANGE", "binance"),
            fee_pct=settings.FEE_PCT,
            metadata=json.dumps({
                "latest_size": round(latest_size, 3),
                "hbias": latest_hbias,
                "trade_size": round(trade_size, 2),
                "leverage": symbol_lev,
                "margin": round(trade_size / symbol_lev, 2),
                "cur_equity": round(self._cur_eq, 2),
            }),
            timestamp=timestamp,
        )

        logger.info(
            "[OPEN] %s %s @ %.6f size=%.2f lev=%dx equity=%.2f hbias=%s",
            side, symbol, close, trade_size, symbol_lev, self._cur_eq, latest_hbias
        )

    def _close_position(self, symbol: str, pos: dict, exit_price: float, reason: str, detail: str = "") -> None:
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
        self._cur_size += (net * 0.3)
        self._cur_size = max(settings.MIN_INVEST, self._cur_size)

        logger.info(
            "[CLOSE] %s reason=%s @ %.6f net=%.2f next_size=%.2f equity=%.2f %s",
            symbol, reason, exit_price, net, self._cur_size, self._cur_eq, detail
        )

    def on_price_alert_message(self, msg: dict) -> None:
        pass
