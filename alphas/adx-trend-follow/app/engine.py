import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import compute_adx, get_candle_seconds, strategy_filter_signal
from base.engine import BaseEngine
from base.symbol_utils import get_binance_perp_symbols

logger = logging.getLogger(__name__)


class ADXTrendFollowEngine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}

    def _has_open_positions(self) -> bool:
        return bool(self._open_positions)

    def get_required_channels(self) -> list[str]:
        return [f"kline:{self.config.TF}"]

    def _get_warmup_symbols(self) -> list[str]:
        all_symbols = get_binance_perp_symbols()
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
                        "pos": dict(pos),
                    }

        to_close: list[dict] = []
        to_modify: list[dict] = []
        to_remove: list[str] = []

        for symbol, snap in snapshots.items():
            close = snap["close"]
            low = snap["low"]
            high = snap["high"]
            pos = snap["pos"]
            side = pos["side"]
            entry = pos["entry"]
            current_sl = pos["sl"]
            current_tp = pos["tp"]
            position_id = pos["position_id"]
            bar_count = pos["bar_count"]
            be_activated = pos["be_activated"]

            sl_hit = (side == "LONG" and low <= current_sl) or (side == "SHORT" and high >= current_sl)
            if sl_hit:
                fill = low if side == "LONG" else high
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": fill,
                        "reason": "SL_HIT",
                        "metadata": json.dumps({
                            "close_model": "candle_fallback_conservative",
                            "reason": "SL_HIT",
                            "stop_price": current_sl,
                            "trigger_price": fill,
                            "raw_fill_price": fill,
                            "candle_high": high,
                            "candle_low": low,
                            "tf": self.config.TF,
                            "source": "kline",
                        }),
                    }
                )
                to_remove.append(symbol)
                continue

            tp_hit = (side == "LONG" and high >= current_tp) or (side == "SHORT" and low <= current_tp)
            if tp_hit:
                trigger = high if side == "LONG" else low
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": current_tp,
                        "reason": "TP_CAP",
                        "metadata": json.dumps({
                            "close_model": "candle_fallback_conservative",
                            "reason": "TP_CAP",
                            "stop_price": current_tp,
                            "trigger_price": trigger,
                            "raw_fill_price": current_tp,
                            "candle_high": high,
                            "candle_low": low,
                            "tf": self.config.TF,
                            "source": "kline",
                        }),
                    }
                )
                to_remove.append(symbol)
                continue

            new_bar_count = bar_count + 1
            if new_bar_count >= self.config.MAX_HOLD_CANDLES:
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": close,
                        "reason": "MAX_HOLD",
                        "metadata": None,
                    }
                )
                to_remove.append(symbol)
                continue

            new_sl = current_sl
            new_be = be_activated

            if side == "LONG":
                if not be_activated and close >= entry * (1 + self.config.BE_TRIGGER_PCT):
                    new_sl = max(new_sl, entry)
                    new_be = True
                if new_be:
                    new_sl = max(new_sl, close * (1 - self.config.TRAIL_DIST_PCT))
            else:
                if not be_activated and close <= entry * (1 - self.config.BE_TRIGGER_PCT):
                    new_sl = min(new_sl, entry)
                    new_be = True
                if new_be:
                    new_sl = min(new_sl, close * (1 + self.config.TRAIL_DIST_PCT))

            self._open_positions[symbol]["bar_count"] = new_bar_count
            self._open_positions[symbol]["be_activated"] = new_be

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
                metadata=item.get("metadata"),
            )
            logger.info("[CLOSE] %s reason=%s @ %s", item["symbol"], item["reason"], item["exit_price"])

        for symbol in to_remove:
            self._open_positions.pop(symbol, None)
        if to_remove:
            self.mark_positions_changed()

    async def _scan_new_signals(self) -> None:
        if not self.can_open_new_trades():
            return
        if len(self._open_positions) >= self.config.MAX_CONCURRENT_POSITIONS:
            return

        snapshot_rows = []
        async with self.data_lock:
            btc_sd = self.symbol_data.get("BTCUSDT", {}).get(self.config.TF)
            if btc_sd is None or len(btc_sd.price_list) < settings.ADX_PERIOD * 2:
                return

            btc_pl = list(btc_sd.price_list)
            btc_hl = list(btc_sd.high_list)
            btc_ll = list(btc_sd.low_list)

            adx_btc = compute_adx(btc_hl, btc_ll, btc_pl, settings.ADX_PERIOD)
            if adx_btc < settings.ADX_THRESHOLD:
                return

            for symbol, tf_map in self.symbol_data.items():
                if symbol == "BTCUSDT" or symbol in self._open_positions:
                    continue
                sd = tf_map.get(self.config.TF)
                if not sd or not sd.price_list or not sd.volume_list:
                    continue
                snapshot_rows.append(
                    {
                        "symbol": symbol,
                        "price_list": list(sd.price_list),
                        "volume_list": list(sd.volume_list),
                        "high_list": list(sd.high_list),
                        "low_list": list(sd.low_list),
                    }
                )

        signals = []
        for row in snapshot_rows:
            signal = strategy_filter_signal(
                symbol=row["symbol"],
                price_list=row["price_list"],
                volume_list=row["volume_list"],
                high_list=row["high_list"],
                low_list=row["low_list"],
                btc_price_list=btc_pl,
                btc_high_list=btc_hl,
                btc_low_list=btc_ll,
            )
            if signal:
                signals.append(signal)

        signals.sort(key=lambda item: item.get("vol_spike", 0), reverse=True)
        available_slots = self.config.MAX_CONCURRENT_POSITIONS - len(self._open_positions)

        for signal in signals[:available_slots]:
            symbol = signal["symbol"]
            if symbol in self._open_positions:
                continue

            side = signal["recommend"]
            entry = signal["entry"]
            position_id = str(uuid.uuid4())
            if side == "LONG":
                sl = entry * (1 - self.config.INITIAL_SL_PCT)
                tp = entry * (1 + self.config.TP_CAP_PCT)
            else:
                sl = entry * (1 + self.config.INITIAL_SL_PCT)
                tp = entry * (1 - self.config.TP_CAP_PCT)

            qty = self.config.INVEST_PER_TRADE * self.config.LEVERAGE / entry
            timestamp = datetime.now(timezone.utc).isoformat()

            self.push_signal(
                "OPEN",
                symbol=symbol,
                side=side,
                entry=entry,
                qty=qty,
                tp=tp,
                sl=sl,
                leverage=self.config.LEVERAGE,
                position_id=position_id,
                exchange=self.config.EXCHANGE,
                fee_pct=self.config.FEE_PCT,
                metadata=json.dumps(
                    {
                        "vol_spike": signal.get("vol_spike"),
                        "price_move": signal.get("price_move"),
                        "btc_adx": signal.get("btc_adx"),
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
                "bar_count": 0,
                "be_activated": False,
            }
            self.mark_positions_changed()
            logger.info("[SIGNAL] OPEN %s %s @ %s sl=%.4f tp=%.4f", side, symbol, entry, sl, tp)
