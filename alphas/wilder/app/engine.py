import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import get_candle_seconds, wilder_filter_signal
from base.engine import BaseEngine
from base.models import SymbolData
from base.symbol_utils import get_top_n_binance_perps

logger = logging.getLogger(__name__)


class WilderEngine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}

    def get_required_channels(self) -> list[str]:
        return [f"kline:{self.config.TF}"]

    def _get_warmup_symbols(self) -> list[str]:
        all_symbols = get_top_n_binance_perps(settings.TOP_N_COINS)
        return [s for s in all_symbols if not self._is_blacklisted(s)]

    async def scan_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self._wait_until_next_candle_offset()
                if self.shutdown_event.is_set():
                    break

                await self._manage_positions()
                await self._scan_new_signals()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scan error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    async def _wait_until_next_candle_offset(self) -> None:
        candle_len = get_candle_seconds(self.config.TF)
        now = time.time()
        next_candle = (int(now // candle_len) + 1) * candle_len
        target = next_candle + self.config.OFFSET_CANDLE_SEC
        wait = target - now
        if wait > 0:
            await asyncio.sleep(wait)

    def _compute_current_atr(self, sd: SymbolData) -> float:
        period = settings.ATR_PERIOD
        if len(sd.price_list) < period:
            return 0.0
        df = pd.DataFrame(
            {
                "high": sd.high_list[-period * 3:],
                "low": sd.low_list[-period * 3:],
                "close": sd.price_list[-period * 3:],
            }
        )
        atr_series = ta.atr(high=df["high"], low=df["low"], close=df["close"], length=period)
        if atr_series is None or atr_series.empty:
            return 0.0
        value = atr_series.iloc[-1]
        return float(value) if pd.notna(value) else 0.0

    async def _manage_positions(self) -> None:
        if not self._open_positions:
            return

        snapshots = {}
        async with self.data_lock:
            for symbol, pos in self._open_positions.items():
                sd = self.symbol_data.get(symbol, {}).get(self.config.TF)
                if sd and sd.price_list and sd.low_list and sd.high_list:
                    snapshots[symbol] = {
                        "close": sd.price_list[-1],
                        "low": sd.low_list[-1],
                        "high": sd.high_list[-1],
                        "atr": self._compute_current_atr(sd),
                        "pos": dict(pos),
                    }

        to_close: list[dict] = []
        to_modify: list[dict] = []
        to_remove: list[str] = []

        for symbol, snap in snapshots.items():
            close = snap["close"]
            low = snap["low"]
            high = snap["high"]
            atr = snap["atr"]
            pos = snap["pos"]
            side = pos["side"]
            current_sl = pos["sl"]
            current_tp = pos["tp"]
            position_id = pos["position_id"]

            sl_hit = (side == "LONG" and low <= current_sl) or (side == "SHORT" and high >= current_sl)
            if sl_hit:
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": current_sl,
                        "reason": "SL_HIT",
                    }
                )
                to_remove.append(symbol)
                continue

            tp_hit = (side == "LONG" and high >= current_tp) or (side == "SHORT" and low <= current_tp)
            if tp_hit:
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": current_tp,
                        "reason": "TP_HIT",
                    }
                )
                to_remove.append(symbol)
                continue

            if atr <= 0:
                continue

            new_sl = current_sl
            if side == "LONG":
                new_sl = max(new_sl, close - settings.TRAIL_ATR_MULT * atr)
            else:
                new_sl = min(new_sl, close + settings.TRAIL_ATR_MULT * atr)

            if new_sl != current_sl:
                self._open_positions[symbol]["sl"] = new_sl
                to_modify.append({"position_id": position_id, "sl": new_sl})

        for item in to_modify:
            self.push_signal("MODIFY", position_id=item["position_id"], sl=item["sl"])
            logger.debug("[MODIFY] position=%s new_sl=%.6f", item["position_id"], item["sl"])

        for item in to_close:
            self.push_signal(
                "CLOSE",
                position_id=item["position_id"],
                exit_price=item["exit_price"],
                reason=item["reason"],
            )
            logger.info("[CLOSE] %s reason=%s @ %s", item["symbol"], item["reason"], item["exit_price"])

        for symbol in to_remove:
            self._open_positions.pop(symbol, None)

    async def _scan_new_signals(self) -> None:
        if len(self._open_positions) >= self.config.MAX_CONCURRENT_POSITIONS:
            return

        snapshot_rows = []
        async with self.data_lock:
            for symbol, tf_map in self.symbol_data.items():
                if symbol in self._open_positions:
                    continue
                sd = tf_map.get(self.config.TF)
                if not sd or not sd.price_list or not sd.high_list or not sd.low_list:
                    continue
                snapshot_rows.append(
                    {
                        "symbol": symbol,
                        "price_list": list(sd.price_list),
                        "high_list": list(sd.high_list),
                        "low_list": list(sd.low_list),
                    }
                )

        signals = []
        for row in snapshot_rows:
            signal = wilder_filter_signal(
                symbol=row["symbol"],
                price_list=row["price_list"],
                high_list=row["high_list"],
                low_list=row["low_list"],
            )
            if signal:
                signals.append(signal)

        available_slots = self.config.MAX_CONCURRENT_POSITIONS - len(self._open_positions)
        for signal in signals[:available_slots]:
            symbol = signal["symbol"]
            if symbol in self._open_positions:
                continue

            side = signal["recommend"]
            entry = signal["entry"]
            sl = signal["sl"]
            tp = signal["tp"]
            atr = signal["atr"]
            position_id = str(uuid.uuid4())
            quantity = self.config.INVEST_PER_TRADE * self.config.LEVERAGE / entry
            timestamp = datetime.now(timezone.utc).isoformat()

            self.push_signal(
                "OPEN",
                symbol=symbol,
                side=side,
                entry=entry,
                qty=quantity,
                tp=tp,
                sl=sl,
                leverage=self.config.LEVERAGE,
                position_id=position_id,
                exchange=self.config.EXCHANGE,
                fee_pct=self.config.FEE_PCT,
                metadata=json.dumps(
                    {
                        "regime": signal.get("regime"),
                        "adx": signal.get("adx"),
                        "plus_di": signal.get("plus_di"),
                        "minus_di": signal.get("minus_di"),
                        "rsi_curr": signal.get("rsi_curr"),
                        "atr": atr,
                    }
                ),
                timestamp=timestamp,
            )

            self._open_positions[symbol] = {
                "position_id": position_id,
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp": tp,
            }
            logger.info(
                "[SIGNAL] OPEN %s %s @ %.4f sl=%.4f tp=%.4f atr=%.4f regime=%s",
                side,
                symbol,
                entry,
                sl,
                tp,
                atr,
                signal.get("regime"),
            )
