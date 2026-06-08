import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import HyperTurboSignal, compute_hyper_turbo_signal
from base.engine import BaseEngine

logger = logging.getLogger(__name__)


class HyperTurboEngine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}
        self._last_entry_signal: tuple[int, str] | None = None
        self._columns_config_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")

    def get_required_channels(self) -> list[str]:
        return list(dict.fromkeys([f"kline:{settings.TF}", "kline:1m"]))

    def _get_warmup_symbols(self) -> list[str]:
        return [] if self._is_blacklisted(settings.SYMBOL) else [settings.SYMBOL]

    def _has_open_positions(self) -> bool:
        return bool(self._open_positions)

    async def scan_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self._process_symbol()
                await asyncio.sleep(settings.SIGNAL_REFRESH_SEC)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scan error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    async def _process_symbol(self) -> None:
        async with self.data_lock:
            tf_data = self.symbol_data.get(settings.SYMBOL, {}).get(settings.TF)
            if not tf_data or len(tf_data.price_list) < settings.SIGNAL_PERIOD + 1:
                return
            closes = list(tf_data.price_list)
            signal_bar_time = int(tf_data.time_list[-1])
            one_minute = self.symbol_data.get(settings.SYMBOL, {}).get("1m")
            execution_price = (
                float(one_minute.price_list[-1])
                if one_minute and one_minute.price_list
                else float(tf_data.price_list[-1])
            )

        signal = compute_hyper_turbo_signal(
            closes,
            period=settings.SIGNAL_PERIOD,
            tp_multiplier=settings.TP_MULTIPLIER,
        )
        if signal is not None:
            self._apply_signal(signal, signal_bar_time, execution_price)

    def _apply_signal(
        self,
        signal: HyperTurboSignal,
        signal_bar_time: int,
        execution_price: float,
    ) -> None:
        symbol = settings.SYMBOL
        pos = self._open_positions.get(symbol)

        if pos:
            reverse = (
                (pos["side"] == "LONG" and signal.go_short)
                or (pos["side"] == "SHORT" and signal.go_long)
            )
            tp_signal = (
                (pos["side"] == "LONG" and signal.tp_long_signal)
                or (pos["side"] == "SHORT" and signal.tp_short_signal)
            )

            if reverse:
                self._close_position(pos, execution_price, "REVERSE_SIGNAL")
                pos = None
            elif tp_signal and pos.get("last_tp_signal_bar_time") != signal_bar_time:
                self._take_profit_leg(pos, execution_price, signal_bar_time)
                pos = self._open_positions.get(symbol)

        if (
            signal.recommend
            and symbol not in self._open_positions
            and self.can_open_new_trades()
            and len(self._open_positions) < settings.MAX_CONCURRENT_POSITIONS
        ):
            dedupe_key = (signal_bar_time, signal.recommend)
            if self._last_entry_signal != dedupe_key:
                self._open_position(signal, signal_bar_time, execution_price)
                self._last_entry_signal = dedupe_key

    def _open_position(
        self,
        signal: HyperTurboSignal,
        signal_bar_time: int,
        entry: float,
    ) -> None:
        if entry <= 0:
            return
        qty = settings.INVEST_PER_TRADE * settings.LEVERAGE / entry
        position_id = str(uuid.uuid4())
        metadata = self._signal_metadata(signal)
        metadata["signal_bar_time"] = signal_bar_time

        self._open_positions[settings.SYMBOL] = {
            "position_id": position_id,
            "side": signal.recommend,
            "entry": entry,
            "initial_qty": qty,
            "remaining_qty": qty,
            "tp_hits": 0,
            "be_active": False,
            "last_tp_signal_bar_time": None,
        }
        self.mark_positions_changed()
        self.push_signal(
            "OPEN",
            symbol=settings.SYMBOL,
            side=signal.recommend,
            entry=entry,
            qty=qty,
            leverage=settings.LEVERAGE,
            position_id=position_id,
            exchange=settings.EXCHANGE,
            fee_pct=settings.FEE_PCT,
            metadata=json.dumps(metadata),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        logger.info("[OPEN] %s %s @ %.6f qty=%.8f", signal.recommend, settings.SYMBOL, entry, qty)

    def _take_profit_leg(self, pos: dict, exit_price: float, signal_bar_time: int) -> None:
        tp_hits = int(pos["tp_hits"])
        if tp_hits == 0:
            close_qty = min(pos["initial_qty"] * 0.75, pos["remaining_qty"])
            self._partial_close(pos, close_qty, exit_price, "TP1", signal_bar_time)
            pos["tp_hits"] = 1
            pos["be_active"] = True
            pos["last_tp_signal_bar_time"] = signal_bar_time
            self.push_signal("MODIFY", position_id=pos["position_id"], sl=pos["entry"])
            logger.info("[BE] %s activated @ %.6f", settings.SYMBOL, pos["entry"])
        elif tp_hits == 1:
            close_qty = min(pos["initial_qty"] * 0.125, pos["remaining_qty"])
            self._partial_close(pos, close_qty, exit_price, "TP2", signal_bar_time)
            pos["tp_hits"] = 2
            pos["last_tp_signal_bar_time"] = signal_bar_time
        else:
            self._close_position(pos, exit_price, "TP3", signal_bar_time)

    def _partial_close(
        self,
        pos: dict,
        qty: float,
        exit_price: float,
        reason: str,
        signal_bar_time: int,
    ) -> None:
        if qty <= 0:
            return
        self.push_signal(
            "CLOSE",
            position_id=pos["position_id"],
            exit_price=exit_price,
            qty=qty,
            reason=reason,
            metadata=json.dumps({
                "close_model": "hyper_turbo_signal",
                "exit_signal_bar_time": signal_bar_time,
                "reason": reason,
            }),
        )
        pos["remaining_qty"] = max(pos["remaining_qty"] - qty, 0.0)
        logger.info(
            "[%s] %s @ %.6f qty=%.8f remaining=%.8f",
            reason, settings.SYMBOL, exit_price, qty, pos["remaining_qty"],
        )

    def _close_position(
        self,
        pos: dict,
        exit_price: float,
        reason: str,
        signal_bar_time: int | None = None,
    ) -> None:
        self.push_signal(
            "CLOSE",
            position_id=pos["position_id"],
            exit_price=exit_price,
            reason=reason,
            metadata=json.dumps({
                "close_model": "hyper_turbo_signal" if reason != "BE" else "price_alert_side_aware",
                "exit_signal_bar_time": signal_bar_time,
                "reason": reason,
            }),
        )
        self._open_positions.pop(settings.SYMBOL, None)
        self.mark_positions_changed()
        logger.info("[CLOSE] %s reason=%s @ %.6f", settings.SYMBOL, reason, exit_price)

    def on_price_alert_message(self, msg: dict) -> None:
        pos = self._open_positions.get(settings.SYMBOL)
        if not pos or not pos.get("be_active"):
            return
        trigger = self._trigger_price(pos["side"], msg)
        if trigger is None:
            return
        hit = (
            pos["side"] == "LONG" and trigger <= pos["entry"]
        ) or (
            pos["side"] == "SHORT" and trigger >= pos["entry"]
        )
        if hit:
            self._close_position(pos, trigger, "BE")

    async def _manage_positions(self) -> None:
        pos = self._open_positions.get(settings.SYMBOL)
        if not pos or not pos.get("be_active"):
            return

        async with self.data_lock:
            sd = self.symbol_data.get(settings.SYMBOL, {}).get("1m")
            if not sd or not sd.open_list or not sd.high_list or not sd.low_list:
                return
            raw_open = float(sd.open_list[-1])
            raw_high = float(sd.high_list[-1])
            raw_low = float(sd.low_list[-1])

        stop = pos["entry"]
        if pos["side"] == "LONG":
            exit_price = raw_open if raw_open <= stop else stop if raw_low <= stop else None
        else:
            exit_price = raw_open if raw_open >= stop else stop if raw_high >= stop else None
        if exit_price is not None:
            self._close_position(pos, exit_price, "BE")

    @staticmethod
    def _signal_metadata(signal: HyperTurboSignal) -> dict:
        return {
            "tf": settings.TF,
            "trend": signal.trend,
            "basis": signal.basis,
            "dev": signal.dev,
            "upper": signal.upper,
            "lower": signal.lower,
            "upper_tp": signal.upper_tp,
            "lower_tp": signal.lower_tp,
        }
