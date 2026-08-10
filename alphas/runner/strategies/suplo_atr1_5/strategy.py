"""Native runner implementation of the ``suplo_ATR1_5`` alpha preset.

Reference: docs/suplo_ATR1_5.py.

Rules (from the reference docstring):
  1. Entry: Supertrend(3, 10) on RAW 15m candles. A flip to downtrend (1)
     opens SHORT, a flip to uptrend (-1) opens LONG (same as the other
     suplo/supertrend family members).
  2. Activation gate: the trailing take-profit only activates once the peak
     profit reaches ``>= atr_multiplier * ATR(10)`` (the 15m ATR captured at
     entry). Until that gate is met the position is held with no trailing
     level and exits only on a 15m Supertrend flip.
  3. Exit: trailing take-profit is simulated minute-by-minute on 1m candles
     with a strict causal order per minute: (1) gap check at open, (2) check
     the trailing level from the PREVIOUS minute against low/high, (3) only
     if not exited, update the peak and compute the next trailing level,
     which takes effect from the following minute. Exit prices include a
     reference slippage (0.0001).

After a trailing exit the strategy stands in cash until the next 15m flip.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from typing import Any

from runner.strategy.base import Strategy

M1_MS = 60 * 1000
M15_MS = 15 * 60 * 1000
POSITION_NAMESPACE = uuid.UUID("28a8f6d8-1478-4e48-a9c8-cb875cdcaf8e")

DOWNTREND = 1  # supertrend below price -> SHORT
UPTREND = -1  # supertrend above price -> LONG
REF_SLIPPAGE = 0.0001


@dataclass(frozen=True)
class CandleSeries:
    opens: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    closes: tuple[float, ...]
    times: tuple[int, ...]


def _atr_rma(
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    closes: tuple[float, ...],
    period: int = 10,
) -> list[float]:
    """Wilder's RMA ATR, matching the reference ``calculate_atr`` (ta.atr(10))."""
    if not closes:
        return []
    ranges = [highs[0] - lows[0]]
    for index in range(1, len(closes)):
        ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    alpha = 1.0 / period
    result = [ranges[0]]
    for value in ranges[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def compute_supertrend_directions(
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    closes: tuple[float, ...],
    factor: float = 3.0,
    period: int = 10,
) -> list[int]:
    """Supertrend direction array (1 = downtrend, -1 = uptrend)."""
    atr = _atr_rma(highs, lows, closes, period=period)
    n = len(closes)
    hl2 = [(highs[i] + lows[i]) / 2.0 for i in range(n)]
    basic_ub = [hl2[i] + factor * atr[i] for i in range(n)]
    basic_lb = [hl2[i] - factor * atr[i] for i in range(n)]

    final_ub = [0.0] * n
    final_lb = [0.0] * n
    direction = [1] * n
    if n:
        final_ub[0] = basic_ub[0]
        final_lb[0] = basic_lb[0]

    for i in range(1, n):
        if basic_ub[i] < final_ub[i - 1] or closes[i - 1] > final_ub[i - 1]:
            final_ub[i] = basic_ub[i]
        else:
            final_ub[i] = final_ub[i - 1]

        if basic_lb[i] > final_lb[i - 1] or closes[i - 1] < final_lb[i - 1]:
            final_lb[i] = basic_lb[i]
        else:
            final_lb[i] = final_lb[i - 1]

        if direction[i - 1] == 1:
            direction[i] = -1 if closes[i] > final_ub[i] else 1
        else:
            direction[i] = 1 if closes[i] < final_lb[i] else -1
    return direction


def process_long_minute_atr1(
    minute_open: float,
    minute_high: float,
    minute_low: float,
    entry_price: float,
    best_high: float,
    active_trailing: float | None,
    min_atr_profit: float,
    retrace_pct: float = 0.05,
    slippage_pct: float = REF_SLIPPAGE,
) -> tuple[bool, float | None, float, float | None]:
    """Port of the reference ``process_long_minute_atr1``.

    Returns ``(exited, exit_price, updated_best_high, next_trailing)``.
    Trailing only activates once ``peak_profit >= min_atr_profit``.
    """
    if active_trailing is not None:
        if minute_open <= active_trailing:
            exit_price = minute_open * (1.0 - slippage_pct)
            return True, exit_price, best_high, None
        if minute_low <= active_trailing:
            exit_price = active_trailing * (1.0 - slippage_pct)
            return True, exit_price, best_high, None

    updated_best_high = max(best_high, minute_high)
    peak_profit = updated_best_high - entry_price
    if peak_profit < min_atr_profit or peak_profit <= 0:
        return False, None, updated_best_high, active_trailing

    retained_pct = 1.0 - retrace_pct
    calculated_trailing = entry_price + peak_profit * retained_pct
    if active_trailing is None:
        next_trailing = calculated_trailing
    else:
        next_trailing = max(active_trailing, calculated_trailing)
    return False, None, updated_best_high, next_trailing


def process_short_minute_atr1(
    minute_open: float,
    minute_high: float,
    minute_low: float,
    entry_price: float,
    best_low: float,
    active_trailing: float | None,
    min_atr_profit: float,
    retrace_pct: float = 0.05,
    slippage_pct: float = REF_SLIPPAGE,
) -> tuple[bool, float | None, float, float | None]:
    """Port of the reference ``process_short_minute_atr1``.

    Returns ``(exited, exit_price, updated_best_low, next_trailing)``.
    Trailing only activates once ``peak_profit >= min_atr_profit``.
    """
    if active_trailing is not None:
        if minute_open >= active_trailing:
            exit_price = minute_open * (1.0 + slippage_pct)
            return True, exit_price, best_low, None
        if minute_high >= active_trailing:
            exit_price = active_trailing * (1.0 + slippage_pct)
            return True, exit_price, best_low, None

    updated_best_low = min(best_low, minute_low)
    peak_profit = entry_price - updated_best_low
    if peak_profit < min_atr_profit or peak_profit <= 0:
        return False, None, updated_best_low, active_trailing

    retained_pct = 1.0 - retrace_pct
    calculated_trailing = entry_price - peak_profit * retained_pct
    if active_trailing is None:
        next_trailing = calculated_trailing
    else:
        next_trailing = min(active_trailing, calculated_trailing)
    return False, None, updated_best_low, next_trailing


class SuploAtr1RunnerStrategy(Strategy):
    """Supertrend(3,10) on 15m entries + 1m trailing TP gated by ATR profit.

    ``retrace_pct`` defaults to 0.05 (5% trailing). ``atr_multiplier``
    defaults to 1.0 (activation gate = 1.0 * ATR(10) on the 15m entry candle).
    """

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        self.retrace_pct = float(params.get("retrace_pct", 0.05))
        self.atr_multiplier = float(params.get("atr_multiplier", 1.0))
        self.factor = float(params.get("factor", 3.0))
        self.atr_period = int(params.get("atr_period", 10))
        self.symbol = str(params.get("symbol", "XAUUSDT")).upper()
        self.exchange = str(params.get("exchange", "binance"))
        self.capital = float(params.get("capital", 10_000.0))
        self.leverage = float(params.get("leverage", 10.0))
        self.position_fraction = float(params.get("position_fraction", 1.0))
        self.fee_pct = float(params.get("fee_pct", 0.0005))
        self.m15_warmup_bars = int(params.get("warmup_bars", 320))
        self.m1_warmup_bars = int(params.get("m1_warmup_bars", 60))
        self.retain_bars = int(params.get("retain_bars", self.m15_warmup_bars))
        self.min_m15_bars = int(params.get("min_m15_bars", 80))
        self.timestamp_semantics = str(
            params.get("timestamp_semantics", "open")
        ).lower()
        if self.timestamp_semantics not in {"open", "close"}:
            raise ValueError("timestamp_semantics must be 'open' or 'close'")
        self._last_trend_dir: int = 0
        self._last_15m_open: int | None = None
        self._last_1m_open: int | None = None
        self._pending_15m_open: int | None = None
        self._pending_1m_open: int | None = None
        self._positions: dict[str, dict[str, Any]] = self._load_positions()

    # ------------------------------------------------------------------ runner API

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        return ["kline:15m", "kline:1m"]

    def get_required_channels_instance(self) -> list[str]:
        return self.__class__.get_required_channels(self.params)

    def get_warmup_symbols(self) -> list[str]:
        return [self.symbol]

    def get_warmup_tfs(self) -> list[str]:
        return ["15m", "1m"]

    def get_warmup_bars(self, tf: str) -> int:
        return self.m1_warmup_bars if tf == "1m" else self.m15_warmup_bars

    def get_retain_bars(self, tf: str) -> int:
        return max(self.get_warmup_bars(tf), self.retain_bars)

    async def _shared_panel_bundle(self):
        return None

    # ------------------------------------------------------------------- event flow

    @staticmethod
    def _timestamp_ms(value: int) -> int:
        value = int(value)
        return value * 1000 if abs(value) < 1_000_000_000_000 else value

    def _bar_open_ms(self, value: int, timeframe_ms: int) -> int:
        value_ms = self._timestamp_ms(value)
        if self.timestamp_semantics == "close":
            close_boundary = value_ms + 1 if value_ms % 1000 == 999 else value_ms
            value_ms = close_boundary - timeframe_ms
        return value_ms

    def should_scan_after_event(
        self, kind: str, symbol: str | None = None, tf: str | None = None
    ) -> bool:
        if kind != "kline" or symbol != self.symbol or not self.ctx.state.ready:
            return False
        latest = self.ctx.cache.get_latest_timestamp(self.symbol, tf)
        if latest is None:
            return False
        if tf == "15m":
            open_ms = self._bar_open_ms(latest, M15_MS)
            if self._last_15m_open is not None and open_ms <= self._last_15m_open:
                return False
            self._pending_15m_open = open_ms
            return True
        if tf == "1m":
            open_ms = self._bar_open_ms(latest, M1_MS)
            if self._last_1m_open is not None and open_ms <= self._last_1m_open:
                return False
            self._pending_1m_open = open_ms
            return True
        return False

    async def scan(self) -> None:
        if self._pending_15m_open is not None:
            await self._scan_15m(self._pending_15m_open)
            self._pending_15m_open = None
        if self._pending_1m_open is not None:
            await self._scan_1m(self._pending_1m_open)
            self._pending_1m_open = None
        self._persist_positions()

    async def manage_positions(self) -> None:
        self._sync_price_alerts()

    # ---------------------------------------------------------------- state helpers

    def _load_positions(self) -> dict[str, dict[str, Any]]:
        source = self.ctx.load_authoritative_positions()
        if source is None:
            source = self.ctx.load_positions()
        if not isinstance(source, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, value in source.items():
            if not isinstance(value, dict):
                continue
            runtime = value.get("strategy_runtime", {})
            owner_alpha = value.get("alpha_id") or (
                runtime.get("alpha_id") if isinstance(runtime, dict) else None
            )
            owner_version = value.get("version") or (
                runtime.get("version") if isinstance(runtime, dict) else None
            )
            if (
                str(value.get("symbol", "")).upper() == self.symbol
                and str(owner_alpha or "") == self.alpha_id
                and str(owner_version or "") == self.version
            ):
                position_id = str(value.get("position_id") or key)
                value = dict(value)
                value["position_id"] = position_id
                result[position_id] = value
        return result

    def _persist_positions(self) -> None:
        if self._positions:
            self.ctx.save_positions(self._positions)
        else:
            self.ctx.clear_positions()
        self._sync_price_alerts()

    def _sync_price_alerts(self) -> None:
        if self.ctx.price_alerts is not None:
            self.ctx.price_alerts.sync({self.symbol} if self._positions else set())

    def _existing_position(self) -> dict[str, Any] | None:
        if not self._positions:
            return None
        return next(iter(self._positions.values()))

    # --------------------------------------------------------------- 15m entry scan

    async def _scan_15m(self, candle_open_ms: int) -> None:
        series = self._series("15m", self.get_retain_bars("15m"))
        if (
            len(series.closes) < self.min_m15_bars
            or not series.times
            or series.times[-1] != candle_open_ms
        ):
            return
        if self._last_15m_open is None:
            # Fresh (re)start: adopt the current trend and any open position
            # without replaying history.
            self._last_15m_open = candle_open_ms
            position = self._existing_position()
            if position is not None:
                position["last_trend_dir"] = (
                    UPTREND if position["side"] == "LONG" else DOWNTREND
                )
                self._last_trend_dir = int(position["last_trend_dir"])
            else:
                self._last_trend_dir = self._current_direction(series)
            return
        self._last_15m_open = candle_open_ms
        directions = compute_supertrend_directions(
            series.highs,
            series.lows,
            series.closes,
            factor=self.factor,
            period=self.atr_period,
        )
        d_curr = directions[-1]
        if d_curr == self._last_trend_dir:
            return
        self._last_trend_dir = d_curr
        close = series.closes[-1]
        position = self._existing_position()
        if position is not None:
            if (position["side"] == "LONG" and d_curr == DOWNTREND) or (
                position["side"] == "SHORT" and d_curr == UPTREND
            ):
                await self._close_position(
                    position, "TREND_FLIP", close, candle_open_ms
                )
                position = None
            else:
                return
        side = "SHORT" if d_curr == DOWNTREND else "LONG"
        atr = _atr_rma(series.highs, series.lows, series.closes, period=self.atr_period)
        min_atr_profit = atr[-1] * self.atr_multiplier if atr else 0.0
        await self._open_if_allowed(side, close, candle_open_ms, min_atr_profit)

    def _current_direction(self, series: CandleSeries) -> int:
        directions = compute_supertrend_directions(
            series.highs,
            series.lows,
            series.closes,
            factor=self.factor,
            period=self.atr_period,
        )
        return directions[-1] if directions else 0

    @staticmethod
    def _atr_of_series(series: CandleSeries, period: int) -> float:
        atr = _atr_rma(series.highs, series.lows, series.closes, period=period)
        return atr[-1] if atr else 0.0

    # ---------------------------------------------------------------- 1m trailing

    async def _scan_1m(self, candle_open_ms: int) -> None:
        series = self._series("1m", self.get_retain_bars("1m"))
        if not series.times or series.times[-1] != candle_open_ms:
            return
        self._last_1m_open = candle_open_ms
        position = self._existing_position()
        if position is None:
            return
        side = str(position["side"])
        entry = float(position["entry"])
        min_atr_profit = float(position.get("min_atr_profit", 0.0))
        o, h, low = series.opens[-1], series.highs[-1], series.lows[-1]
        if side == "LONG":
            best = float(position.get("best_high", entry))
            active = position.get("active_trailing")
            active = float(active) if active is not None else None
            exited, exit_price, best_high, next_trailing = process_long_minute_atr1(
                o, h, low, entry, best, active, min_atr_profit, self.retrace_pct
            )
            position["best_high"] = best_high
        else:
            best = float(position.get("best_low", entry))
            active = position.get("active_trailing")
            active = float(active) if active is not None else None
            exited, exit_price, best_low, next_trailing = process_short_minute_atr1(
                o, h, low, entry, best, active, min_atr_profit, self.retrace_pct
            )
            position["best_low"] = best_low
        if exited:
            await self._close_position(
                position, "TRAILING_TP", exit_price, candle_open_ms
            )
            return
        position["active_trailing"] = next_trailing
        position["tp"] = next_trailing

    # ------------------------------------------------------------------ series

    def _series(self, tf: str, bars: int) -> CandleSeries:
        snapshot = self.ctx.cache.snapshot(self.symbol, tf, bars)
        times = tuple(
            self._bar_open_ms(t, M1_MS if tf == "1m" else M15_MS)
            for t in snapshot.times
        )
        return CandleSeries(
            tuple(snapshot.opens),
            tuple(snapshot.highs),
            tuple(snapshot.lows),
            tuple(snapshot.closes),
            times,
        )

    # ------------------------------------------------------------- signal management

    async def _open_if_allowed(
        self, side: str, entry: float, candle_open_ms: int, min_atr_profit: float
    ) -> None:
        if self._existing_position() is not None:
            return
        qty = (
            math.floor(
                (self.capital * self.position_fraction * self.leverage / entry) * 1000
            )
            / 1000
        )
        if qty < 0.001:
            return
        position_id = str(
            uuid.uuid5(
                POSITION_NAMESPACE,
                f"{self.alpha_id}|{self.version}|{candle_open_ms}|{side}",
            )
        )
        position = {
            "position_id": position_id,
            "symbol": self.symbol,
            "alpha_id": self.alpha_id,
            "version": self.version,
            "side": side,
            "entry": entry,
            "qty": qty,
            "tp": None,
            "sl": None,
            "entry_candle_open_ms": candle_open_ms,
            "last_trend_dir": UPTREND if side == "LONG" else DOWNTREND,
            "best_high": entry if side == "LONG" else 0.0,
            "best_low": entry if side == "SHORT" else 0.0,
            "active_trailing": None,
            "min_atr_profit": min_atr_profit,
        }
        metadata = {
            "retrace_pct": self.retrace_pct,
            "atr_multiplier": self.atr_multiplier,
            "min_atr_profit": min_atr_profit,
            "strategy_runtime": position,
            "allow_duplicate_position": False,
        }
        await self.ctx.emit_signal(
            "OPEN",
            position_id=position_id,
            symbol=self.symbol,
            side=side,
            entry=entry,
            qty=qty,
            tp=None,
            sl=None,
            leverage=self.leverage,
            exchange=self.exchange,
            fee_pct=self.fee_pct,
            tf="15m",
            signal_candle_open_ms=candle_open_ms,
            metadata=json.dumps(metadata, sort_keys=True),
        )
        self._positions[position_id] = position

    async def _close_position(
        self,
        position: dict[str, Any],
        reason: str,
        exit_price: float | None,
        candle_open_ms: int,
    ) -> None:
        await self.ctx.emit_signal(
            "CLOSE",
            position_id=position["position_id"],
            symbol=self.symbol,
            reason=reason,
            exit_price=exit_price,
            metadata=json.dumps(
                {
                    "source": "suplo_atr1_5",
                    "ref_is_executable": True,
                    "signal_candle_open_ms": candle_open_ms,
                },
                sort_keys=True,
            ),
        )
        self._positions.pop(position["position_id"], None)
