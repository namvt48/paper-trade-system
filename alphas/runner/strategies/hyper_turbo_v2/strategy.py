from __future__ import annotations

import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runner.strategy.base import Strategy


BACKTEST_SYMBOLS: tuple[str, ...] = (
    "CAKEUSDT", "SEIUSDT", "BIOUSDT", "FIDAUSDT", "ORDIUSDT", "INJUSDT",
    "NEARUSDT", "OPUSDT", "AAVEUSDT", "TAOUSDT", "DOGEUSDT", "SUIUSDT",
    "BNBUSDT", "LINKUSDT", "TIAUSDT", "SUPERUSDT", "DYMUSDT", "IDUSDT",
    "BTCUSDT",
)


def _periods(value: str | list[int] | tuple[int, ...]) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(int(item) for item in value)
    return tuple(int(item.strip()) for item in str(value).split(",") if item.strip())


def _load_legacy_strategy_module(alphas_root: Path):
    path = alphas_root / "hyper-turbo-v2" / "app" / "strategy.py"
    spec = importlib.util.spec_from_file_location("hyper_turbo_v2_legacy_strategy", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HyperTurboV2RunnerStrategy(Strategy):
    def __init__(self, alpha_id: str, version: str, params: dict, ctx):
        super().__init__(alpha_id, version, params, ctx)
        self._alphas_root = Path(__file__).resolve().parents[3]
        legacy = _load_legacy_strategy_module(self._alphas_root)
        self._compute_signal = legacy.compute_hyper_turbo_signal
        self.tf = str(params.get("tf", "4h"))
        self.exchange = str(params.get("exchange", "binance"))
        self.symbols = tuple(str(s).upper() for s in params.get("symbols", BACKTEST_SYMBOLS))
        self.periods = _periods(params.get("signal_periods", (20, 30, 50)))
        self.atr_period = int(params.get("atr_period", 14))
        self.daily_ma_period = int(params.get("daily_ma_period", 50))
        self.atr_stop_multiplier = float(params.get("atr_stop_multiplier", 2.5))
        self.catastrophe_pct = float(params.get("catastrophe_pct", 0.25))
        self.capital = float(params.get("capital", 10_000.0))
        self.risk_per_trade = float(params.get("risk_per_trade", 0.005))
        self.leverage_cap = float(params.get("leverage_cap", 20.0))
        self.leverage = int(params.get("leverage", 20))
        self.max_concurrent_positions = int(params.get("max_concurrent_positions", len(self.symbols)))
        self.slippage_pct = float(params.get("slippage_pct", 0.0006))
        self.funding_rate_8h = float(params.get("funding_rate_8h", 0.0001))
        self.fee_pct = float(params.get("fee_pct", 0.0005))
        self.entry_window_sec = float(params.get("entry_window_sec", 60.0))
        self.warmup_bars = int(params.get("warmup_bars", 360))
        self.retain_bars = int(params.get("retain_bars", params.get("data_max_candles", 1000)))
        self.retain_1m_bars = int(params.get("retain_1m_bars", 120))
        self._open_positions: dict[str, dict[str, Any]] = {}
        self._last_signal_bar: dict[str, int] = {}
        self._equity = self.capital

    def get_required_channels(self) -> list[str]:
        return [f"kline:{self.tf}", "kline:1m"]

    def get_warmup_symbols(self) -> list[str]:
        return list(self.symbols)

    def get_warmup_tfs(self) -> list[str]:
        return [self.tf, "1m"]

    def get_warmup_bars(self, tf: str) -> int:
        return self.warmup_bars if tf == self.tf else 1

    def get_retain_bars(self, tf: str) -> int:
        return self.retain_bars if tf == self.tf else self.retain_1m_bars

    async def on_candle(self, symbol: str, tf: str) -> None:
        if tf == "1m":
            await self._manage_symbol(symbol)

    async def scan(self) -> None:
        if not self.ctx.state.ready:
            return
        for symbol in self.symbols:
            await self._process_symbol(symbol)

    async def manage_positions(self) -> None:
        for symbol in list(self._open_positions):
            await self._manage_symbol(symbol)

    async def on_price_alert(self, symbol: str, price: float, side: str) -> None:
        pos = self._open_positions.get(symbol.upper())
        if not pos:
            return
        reason, stop = self._hit_reason(pos, price, price)
        if reason:
            await self._close_position(
                symbol.upper(),
                pos,
                self._cost_adjusted_exit(pos, price),
                reason,
                json.dumps({
                    "close_model": "hyper_turbo_v2",
                    "reason": reason,
                    "stop_price": stop,
                    "trigger_price": price,
                    "ref_is_executable": True,
                }),
            )

    async def _process_symbol(self, symbol: str) -> None:
        snap = self.ctx.cache.snapshot(symbol, self.tf, self.warmup_bars)
        if len(snap.closes) < self.warmup_bars:
            return
        closes = list(snap.closes[:-1])
        highs = list(snap.highs[:-1])
        lows = list(snap.lows[:-1])
        times = list(snap.times[:-1])
        if not times:
            return
        signal_bar_time = int(snap.times[-2])
        execution_bar_time = int(snap.times[-1])
        if self._last_signal_bar.get(symbol) == signal_bar_time:
            return
        signal = self._compute_signal(
            closes,
            highs,
            lows,
            times,
            periods=self.periods,
            atr_period=self.atr_period,
            daily_ma_period=self.daily_ma_period,
        )
        if signal is None:
            return
        self._last_signal_bar[symbol] = signal_bar_time
        execution_open = float(snap.opens[-1])
        market_price = self._latest_market_price(symbol, execution_open)
        await self._apply_signal(
            symbol,
            signal,
            signal_bar_time,
            execution_bar_time,
            execution_open,
            market_price,
            allow_entry=0 <= int(time.time() * 1000) - execution_bar_time <= self.entry_window_sec * 1000,
        )

    async def _apply_signal(
        self,
        symbol: str,
        signal,
        signal_bar_time: int,
        execution_bar_time: int,
        execution_open: float,
        market_price: float,
        allow_entry: bool,
    ) -> None:
        pos = self._open_positions.get(symbol)
        if pos:
            await self._update_trailing_stop(symbol, pos, signal)
            stop_reason, _ = self._hit_reason(pos, market_price, market_price)
            if stop_reason:
                await self._close_position(
                    symbol,
                    pos,
                    self._cost_adjusted_exit(pos, market_price, execution_bar_time),
                    stop_reason,
                    json.dumps({"close_model": "hyper_turbo_v2", "reason": stop_reason, "ref_is_executable": True}),
                )
            else:
                reverse = (pos["side"] == "LONG" and signal.go_short) or (pos["side"] == "SHORT" and signal.go_long)
                if reverse:
                    await self._close_position(
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
            and self.ctx.can_open_trades()
            and len(self._open_positions) < self.max_concurrent_positions
        ):
            await self._open_position(symbol, signal, signal_bar_time, execution_bar_time, execution_open)

    def _latest_market_price(self, symbol: str, fallback: float) -> float:
        one_minute = self.ctx.cache.snapshot(symbol, "1m", 1)
        return float(one_minute.closes[-1]) if one_minute.closes else fallback

    async def _open_position(self, symbol: str, signal, signal_bar_time: int, execution_bar_time: int, raw_open: float) -> None:
        if raw_open <= 0 or signal.risk_atr <= 0 or signal.recommend is None:
            return
        side = signal.recommend
        entry = raw_open * (1 + self.slippage_pct if side == "LONG" else 1 - self.slippage_pct)
        stop_distance = self.atr_stop_multiplier * signal.risk_atr
        risk_units = self._equity * self.risk_per_trade / stop_distance
        cap_units = self._equity * self.leverage_cap / entry
        qty = min(risk_units, cap_units)
        if qty <= 0:
            return
        stop = entry - stop_distance if side == "LONG" else entry + stop_distance
        catastrophe = entry * (1 - self.catastrophe_pct if side == "LONG" else 1 + self.catastrophe_pct)
        position_id = f"{self.alpha_id}:{symbol}:{side}:{execution_bar_time}"
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
        await self.ctx.emit_signal(
            "OPEN",
            symbol=symbol,
            side=side,
            tf=self.tf,
            entry=entry,
            qty=qty,
            sl=stop,
            leverage=self.leverage,
            position_id=position_id,
            exchange=self.exchange,
            fee_pct=self.fee_pct,
            metadata=json.dumps(self._signal_metadata(signal, signal_bar_time, execution_bar_time, catastrophe)),
            signal_candle_open_ms=signal_bar_time,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def _update_trailing_stop(self, symbol: str, pos: dict, signal) -> None:
        if signal.atr <= 0:
            return
        if pos["side"] == "LONG":
            new_stop = max(float(pos["sl"]), signal.close - self.atr_stop_multiplier * signal.atr)
        else:
            new_stop = min(float(pos["sl"]), signal.close + self.atr_stop_multiplier * signal.atr)
        if new_stop == pos["sl"]:
            return
        pos["sl"] = new_stop
        await self.ctx.emit_signal("MODIFY", symbol=symbol, tf=self.tf, position_id=pos["position_id"], sl=new_stop)

    async def _manage_symbol(self, symbol: str) -> None:
        pos = self._open_positions.get(symbol)
        if not pos:
            return
        snap = self.ctx.cache.snapshot(symbol, "1m", self.retain_1m_bars)
        indices = [idx for idx, timestamp in enumerate(snap.times) if timestamp > pos.get("last_managed_ms", 0)]
        if not indices:
            return
        low = min(float(snap.lows[idx]) for idx in indices)
        high = max(float(snap.highs[idx]) for idx in indices)
        latest_ms = int(snap.times[indices[-1]])
        pos["last_managed_ms"] = latest_ms
        reason, stop = self._hit_reason(pos, low, high)
        if not reason:
            return
        open_price = float(snap.opens[indices[0]])
        if pos["side"] == "LONG":
            raw_fill = open_price if open_price <= stop else stop
            trigger = low
        else:
            raw_fill = open_price if open_price >= stop else stop
            trigger = high
        await self._close_position(
            symbol,
            pos,
            self._cost_adjusted_exit(pos, raw_fill, latest_ms + 60_000),
            reason,
            json.dumps({
                "close_model": "hyper_turbo_v2",
                "reason": reason,
                "stop_price": stop,
                "trigger_price": trigger,
                "fill_price": raw_fill,
                "candle_high": high,
                "candle_low": low,
                "ref_is_executable": True,
            }),
        )

    async def _close_position(self, symbol: str, pos: dict, exit_price: float, reason: str, metadata: str | None = None) -> None:
        direction = 1.0 if pos["side"] == "LONG" else -1.0
        gross_pnl = (exit_price - float(pos["entry"])) * float(pos["qty"]) * direction
        fees = (float(pos["entry"]) + exit_price) * float(pos["qty"]) * self.fee_pct
        self._equity = max(0.0, self._equity + gross_pnl - fees)
        await self.ctx.emit_signal(
            "CLOSE",
            symbol=symbol,
            tf=self.tf,
            position_id=pos["position_id"],
            exit_price=exit_price,
            reason=reason,
            metadata=metadata or json.dumps({"close_model": "hyper_turbo_v2", "reason": reason}),
            signal_candle_open_ms=pos.get("last_strategy_candle_ms", ""),
        )
        self._open_positions.pop(symbol, None)

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

    def _cost_adjusted_exit(self, pos: dict, price: float, close_time_ms: int | None = None) -> float:
        side = pos["side"]
        slipped = price * (1 - self.slippage_pct if side == "LONG" else 1 + self.slippage_pct)
        close_time_ms = int(time.time() * 1000) if close_time_ms is None else close_time_ms
        held_ms = max(0, close_time_ms - int(pos.get("opened_at_ms", close_time_ms)))
        funding_intervals = held_ms / (8 * 3_600_000)
        funding_per_unit = float(pos["entry"]) * self.funding_rate_8h * funding_intervals
        return slipped - funding_per_unit if side == "LONG" else slipped + funding_per_unit

    def _signal_metadata(self, signal, signal_bar_time: int, execution_bar_time: int, catastrophe: float) -> dict:
        return {
            "logic_version": "hyper-turbo-v2",
            "tf": self.tf,
            "periods": list(self.periods),
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
            "funding_rate_8h": self.funding_rate_8h,
            "sizing_equity": self._equity,
        }

