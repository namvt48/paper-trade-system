import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import BACKTEST_SYMBOLS, settings
from app.strategy import HyperTurboSignal, compute_hyper_turbo_signal
from base.engine import BaseEngine

logger = logging.getLogger(__name__)


def _periods(value: str) -> tuple[int, ...]:
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


class HyperTurboV2Engine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}
        self._last_signal_bar: dict[str, int] = {}
        self._symbols = list(BACKTEST_SYMBOLS)
        self._periods = _periods(settings.SIGNAL_PERIODS)
        self._equity = settings.CAPITAL
        self._columns_config_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")

    def get_required_channels(self) -> list[str]:
        return [f"kline:{settings.TF}", "kline:1m"]

    def _get_warmup_symbols(self) -> list[str]:
        return self._symbols

    def _has_open_positions(self) -> bool:
        return bool(self._open_positions)

    async def scan_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self._process_all_symbols()
                await asyncio.sleep(settings.SIGNAL_REFRESH_SEC)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scan error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    async def _process_all_symbols(self) -> None:
        snapshots: list[dict] = []
        async with self.data_lock:
            for symbol in self._symbols:
                sd = self.symbol_data.get(symbol, {}).get(settings.TF)
                if not sd or len(sd.price_list) < settings.WARMUP_BARS:
                    continue
                # The newest H4 candle is the execution candle. Indicators only
                # receive candles before it, preserving close[i] -> open[i+1].
                snapshots.append({
                    "symbol": symbol,
                    "closes": list(sd.price_list[:-1]),
                    "highs": list(sd.high_list[:-1]),
                    "lows": list(sd.low_list[:-1]),
                    "times": list(sd.time_list[:-1]),
                    "signal_bar_time": int(sd.time_list[-2]),
                    "execution_bar_time": int(sd.time_list[-1]),
                    "execution_open": float(sd.open_list[-1]),
                    "market_price": self._latest_market_price(symbol, float(sd.open_list[-1])),
                })

        for row in snapshots:
            signal_bar_time = row["signal_bar_time"]
            if self._last_signal_bar.get(row["symbol"]) == signal_bar_time:
                continue
            signal = compute_hyper_turbo_signal(
                row["closes"],
                row["highs"],
                row["lows"],
                row["times"],
                periods=self._periods,
                atr_period=settings.ATR_PERIOD,
                daily_ma_period=settings.DAILY_MA_PERIOD,
            )
            if signal is None:
                continue
            self._last_signal_bar[row["symbol"]] = signal_bar_time
            self._apply_signal(
                row["symbol"],
                signal,
                signal_bar_time,
                row["execution_bar_time"],
                row["execution_open"],
                market_price=row["market_price"],
                allow_entry=(
                    0 <= int(time.time() * 1000) - row["execution_bar_time"]
                    <= settings.ENTRY_WINDOW_SEC * 1000
                ),
            )

    def _apply_signal(
        self,
        symbol: str,
        signal: HyperTurboSignal,
        signal_bar_time: int,
        execution_bar_time: int,
        execution_open: float,
        market_price: float | None = None,
        allow_entry: bool = True,
    ) -> None:
        market_price = execution_open if market_price is None else market_price
        pos = self._open_positions.get(symbol)
        if pos:
            self._update_trailing_stop(symbol, pos, signal)
            stop_reason, _ = self._hit_reason(pos, market_price, market_price)
            if stop_reason:
                self._close_position(
                    symbol,
                    pos,
                    self._cost_adjusted_exit(pos, market_price, execution_bar_time),
                    stop_reason,
                    json.dumps({"close_model": "hyper_turbo_v2", "reason": stop_reason, "ref_is_executable": True}),
                )
            else:
                reverse = (
                    (pos["side"] == "LONG" and signal.go_short)
                    or (pos["side"] == "SHORT" and signal.go_long)
                )
                if reverse:
                    self._close_position(
                        symbol,
                        pos,
                        self._cost_adjusted_exit(pos, market_price, execution_bar_time),
                        "REVERSE",
                        json.dumps({"close_model": "hyper_turbo_v2", "reason": "REVERSE", "ref_is_executable": True}),
                    )

        if (
            allow_entry
            and signal.recommend
            and symbol not in self._open_positions
            and self.can_open_new_trades()
            and len(self._open_positions) < settings.MAX_CONCURRENT_POSITIONS
        ):
            self._open_position(symbol, signal, signal_bar_time, execution_bar_time, execution_open)

    def _latest_market_price(self, symbol: str, fallback: float) -> float:
        one_minute = self.symbol_data.get(symbol, {}).get("1m")
        if one_minute and one_minute.price_list:
            return float(one_minute.price_list[-1])
        return fallback

    def _open_position(
        self,
        symbol: str,
        signal: HyperTurboSignal,
        signal_bar_time: int,
        execution_bar_time: int,
        raw_open: float,
    ) -> None:
        if raw_open <= 0 or signal.risk_atr <= 0 or signal.recommend is None:
            return
        side = signal.recommend
        entry = raw_open * (1 + settings.SLIPPAGE_PCT if side == "LONG" else 1 - settings.SLIPPAGE_PCT)
        stop_distance = settings.ATR_STOP_MULTIPLIER * signal.risk_atr
        risk_units = self._equity * settings.RISK_PER_TRADE / stop_distance
        cap_units = self._equity * settings.LEVERAGE_CAP / entry
        qty = min(risk_units, cap_units)
        if qty <= 0:
            return

        stop = entry - stop_distance if side == "LONG" else entry + stop_distance
        catastrophe = entry * (1 - settings.CATASTROPHE_PCT if side == "LONG" else 1 + settings.CATASTROPHE_PCT)
        position_id = str(uuid.uuid4())
        pos = {
            "position_id": position_id,
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "qty": qty,
            "sl": stop,
            "catastrophe": catastrophe,
            "entry_candle_open_ms": execution_bar_time,
            "signal_candle_close_ms": execution_bar_time,
            "last_strategy_candle_ms": signal_bar_time,
            "last_managed_ms": execution_bar_time,
            "opened_at_ms": execution_bar_time,
        }
        self._open_positions[symbol] = pos
        self.mark_positions_changed()
        self.push_signal(
            "OPEN",
            symbol=symbol,
            side=side,
            entry=entry,
            qty=qty,
            sl=stop,
            leverage=settings.LEVERAGE,
            position_id=position_id,
            exchange=settings.EXCHANGE,
            fee_pct=settings.FEE_PCT,
            metadata=json.dumps(
                self._signal_metadata(signal, signal_bar_time, execution_bar_time, catastrophe, self._equity)
            ),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        logger.info("[OPEN] %s %s @ %.6f qty=%.8f sl=%.6f", side, symbol, entry, qty, stop)

    def _update_trailing_stop(self, symbol: str, pos: dict, signal: HyperTurboSignal) -> None:
        if signal.atr <= 0:
            return
        if pos["side"] == "LONG":
            new_stop = max(float(pos["sl"]), signal.close - settings.ATR_STOP_MULTIPLIER * signal.atr)
        else:
            new_stop = min(float(pos["sl"]), signal.close + settings.ATR_STOP_MULTIPLIER * signal.atr)
        if new_stop == pos["sl"]:
            return
        pos["sl"] = new_stop
        self.push_signal("MODIFY", position_id=pos["position_id"], sl=new_stop)
        logger.info("[TRAIL] %s %s sl=%.6f", pos["side"], symbol, new_stop)

    def _close_position(self, symbol: str, pos: dict, exit_price: float, reason: str, metadata: str | None = None) -> None:
        direction = 1.0 if pos["side"] == "LONG" else -1.0
        gross_pnl = (exit_price - float(pos["entry"])) * float(pos["qty"]) * direction
        fees = (float(pos["entry"]) + exit_price) * float(pos["qty"]) * settings.FEE_PCT
        new_equity = max(0.0, self._equity + gross_pnl - fees)
        self.push_signal(
            "CLOSE",
            position_id=pos["position_id"],
            exit_price=exit_price,
            reason=reason,
            metadata=metadata or json.dumps({"close_model": "hyper_turbo_v2", "reason": reason}),
        )
        self._equity = new_equity
        self._open_positions.pop(symbol, None)
        self.mark_positions_changed()
        logger.info(
            "[CLOSE] %s %s reason=%s @ %.6f equity=%.2f",
            pos["side"], symbol, reason, exit_price, self._equity,
        )

    def on_price_alert_message(self, msg: dict) -> None:
        symbol = str(msg.get("symbol", "")).upper()
        pos = self._open_positions.get(symbol)
        if not pos:
            return
        trigger = self._trigger_price(pos["side"], msg)
        if trigger is None:
            return
        reason, stop = self._hit_reason(pos, trigger, trigger)
        if reason:
            self._close_position(
                symbol,
                pos,
                self._cost_adjusted_exit(pos, trigger),
                reason,
                self._build_close_metadata(
                    reason=reason,
                    stop_price=stop,
                    trigger_price=trigger,
                    tick=msg,
                ),
            )

    async def _manage_positions(self) -> None:
        snapshots: dict[str, dict] = {}
        async with self.data_lock:
            for symbol, pos in self._open_positions.items():
                sd = self.symbol_data.get(symbol, {}).get("1m")
                if not sd or not sd.time_list:
                    continue
                indices = [idx for idx, timestamp in enumerate(sd.time_list) if timestamp > pos.get("last_managed_ms", 0)]
                if not indices:
                    continue
                snapshots[symbol] = {
                    "open": float(sd.open_list[indices[0]]),
                    "high": max(float(sd.high_list[idx]) for idx in indices),
                    "low": min(float(sd.low_list[idx]) for idx in indices),
                    "latest_ms": int(sd.time_list[indices[-1]]),
                }
                pos["last_managed_ms"] = snapshots[symbol]["latest_ms"]

        for symbol, snap in snapshots.items():
            pos = self._open_positions.get(symbol)
            if not pos:
                continue
            reason, stop = self._hit_reason(pos, snap["low"], snap["high"])
            if not reason:
                continue
            if pos["side"] == "LONG":
                raw_fill = snap["open"] if snap["open"] <= stop else stop
                trigger = snap["low"]
            else:
                raw_fill = snap["open"] if snap["open"] >= stop else stop
                trigger = snap["high"]
            self._close_position(
                symbol,
                pos,
                self._cost_adjusted_exit(pos, raw_fill, snap["latest_ms"] + 60_000),
                reason,
                self._candle_close_metadata(
                    reason=reason,
                    stop_price=stop,
                    trigger_price=trigger,
                    fill_price=raw_fill,
                    candle_high=snap["high"],
                    candle_low=snap["low"],
                ),
            )

    def _candle_close_metadata(self, **kwargs) -> str:
        metadata = json.loads(self._build_candle_close_metadata(**kwargs))
        metadata["ref_is_executable"] = True
        return json.dumps(metadata)

    @staticmethod
    def _hit_reason(pos: dict, low: float, high: float) -> tuple[str | None, float]:
        side = pos["side"]
        stop = float(pos["sl"])
        catastrophe = float(pos["catastrophe"])
        if (side == "LONG" and low <= stop) or (side == "SHORT" and high >= stop):
            return "ATR_TRAILING_STOP", stop
        if (side == "LONG" and low <= catastrophe) or (side == "SHORT" and high >= catastrophe):
            return "CATASTROPHE_STOP", catastrophe
        return None, 0.0

    @staticmethod
    def _cost_adjusted_exit(pos: dict, price: float, close_time_ms: int | None = None) -> float:
        side = pos["side"]
        slipped = price * (1 - settings.SLIPPAGE_PCT if side == "LONG" else 1 + settings.SLIPPAGE_PCT)
        close_time_ms = int(time.time() * 1000) if close_time_ms is None else close_time_ms
        held_ms = max(0, close_time_ms - int(pos.get("opened_at_ms", close_time_ms)))
        funding_intervals = held_ms / (8 * 3_600_000)
        funding_per_unit = float(pos["entry"]) * settings.FUNDING_RATE_8H * funding_intervals
        return slipped - funding_per_unit if side == "LONG" else slipped + funding_per_unit

    @staticmethod
    def _signal_metadata(
        signal: HyperTurboSignal,
        signal_bar_time: int,
        execution_bar_time: int,
        catastrophe: float,
        sizing_equity: float,
    ) -> dict:
        return {
            "logic_version": "hyper-turbo-v2",
            "tf": settings.TF,
            "periods": list(_periods(settings.SIGNAL_PERIODS)),
            "period_trends": list(signal.period_trends),
            "period_votes": list(signal.period_votes),
            "basis": signal.basis,
            "dev": signal.dev,
            "upper": signal.upper,
            "lower": signal.lower,
            "atr": signal.atr,
            "risk_atr": signal.risk_atr,
            "htf_ma": signal.htf_ma,
            "atr_rising": signal.atr_rising,
            "htf_pass": signal.htf_pass,
            "catastrophe": catastrophe,
            "signal_bar_time": signal_bar_time,
            "execution_bar_time": execution_bar_time,
            "funding_rate_8h": settings.FUNDING_RATE_8H,
            "sizing_equity": sizing_equity,
        }
