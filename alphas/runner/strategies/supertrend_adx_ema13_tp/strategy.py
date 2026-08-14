"""Supertrend baseline entries with one ADX lower-peak TP per trend cycle.

The runner is a causal port of the supplied Pine Script:

* Supertrend(3, 10) on closed 15m candles opens LONG/SHORT on direction flips.
* DMI/ADX(14, 14) is evaluated on the same closed 15m candles.
* A confirmed ADX pivot high is known only after ``pivot_right`` bars.
* The second, lower ADX peak in the same Supertrend cycle closes the position.
* A cycle can emit at most one ADX TP. After TP the strategy remains in cash
  until the next Supertrend flip.

Runner plumbing, position sizing, persistence and signal execution are kept
from the previous paper-trade implementation.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from typing import Any

from runner.strategy.base import Strategy

M15_MS = 15 * 60 * 1000
POSITION_NAMESPACE = uuid.UUID("9c4e1d2f-6b8a-4e5f-9c3d-2a7b8f1e5d4c")

DOWNTREND = 1  # supertrend below price -> SHORT
UPTREND = -1  # supertrend above price -> LONG


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


def _wilder_rma(values: tuple[float, ...] | list[float], period: int) -> list[float]:
    """Wilder RMA with an SMA seed, matching Pine's DMI/ADX warm-up."""
    n = len(values)
    result = [math.nan] * n
    if period <= 0:
        raise ValueError("period must be positive")
    if n < period:
        return result
    result[period - 1] = sum(values[:period]) / period
    alpha = 1.0 / period
    for i in range(period, n):
        result[i] = alpha * values[i] + (1.0 - alpha) * result[i - 1]
    return result


def compute_dmi_adx(
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    closes: tuple[float, ...],
    di_length: int = 14,
    adx_smoothing: int = 14,
) -> tuple[list[float], list[float], list[float]]:
    """Return ``(+DI, -DI, ADX)`` using TradingView-style Wilder smoothing."""
    n = len(closes)
    if n == 0:
        return [], [], []
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    true_range = [0.0] * n
    true_range[0] = highs[0] - lows[0]
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if up_move > down_move and up_move > 0.0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0.0 else 0.0
        true_range[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    smoothed_tr = _wilder_rma(true_range, di_length)
    smoothed_plus = _wilder_rma(plus_dm, di_length)
    smoothed_minus = _wilder_rma(minus_dm, di_length)
    plus_di = [math.nan] * n
    minus_di = [math.nan] * n
    dx = [math.nan] * n
    for i in range(n):
        tr = smoothed_tr[i]
        if math.isfinite(tr) and tr != 0.0:
            plus_di[i] = 100.0 * smoothed_plus[i] / tr
            minus_di[i] = 100.0 * smoothed_minus[i] / tr
            denominator = plus_di[i] + minus_di[i]
            if denominator != 0.0:
                dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denominator

    adx = [math.nan] * n
    finite_indices = [i for i, value in enumerate(dx) if math.isfinite(value)]
    if len(finite_indices) >= adx_smoothing:
        seed_indices = finite_indices[:adx_smoothing]
        seed_at = seed_indices[-1]
        adx[seed_at] = sum(dx[i] for i in seed_indices) / adx_smoothing
        alpha = 1.0 / adx_smoothing
        for i in range(seed_at + 1, n):
            adx[i] = (
                alpha * dx[i] + (1.0 - alpha) * adx[i - 1]
                if math.isfinite(dx[i])
                else adx[i - 1]
            )
    return plus_di, minus_di, adx


def confirmed_pivot_high(
    values: list[float], confirmation_index: int, left: int, right: int
) -> tuple[int, float] | None:
    """Return ``(pivot_index, value)`` when a pivot is causally confirmed."""
    pivot_index = confirmation_index - right
    if pivot_index - left < 0 or pivot_index + right >= len(values):
        return None
    window = values[pivot_index - left : pivot_index + right + 1]
    pivot = values[pivot_index]
    if not math.isfinite(pivot) or not all(math.isfinite(value) for value in window):
        return None
    if pivot < max(window[:left]) or pivot < max(window[left + 1 :]):
        return None
    return pivot_index, pivot


class _SupertrendAdxPivotRunnerBase(Strategy):
    """Supertrend(3,10) entries plus one ADX lower-peak TP per trend cycle."""

    strategy_type = "supertrend_adx_lower_peak_tp"
    signal_source = "supertrend_adx_lower_peak_tp"

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        self.factor = float(params.get("factor", 3.0))
        self.atr_period = int(params.get("atr_period", 10))
        self.di_length = int(params.get("di_length", 14))
        self.adx_smoothing = int(params.get("adx_smoothing", 14))
        self.pivot_left = int(params.get("pivot_left", 4))
        self.pivot_right = int(params.get("pivot_right", 2))
        self.min_peak_drop = float(params.get("min_peak_drop", 0.0))
        if self.di_length < 1 or self.adx_smoothing < 1:
            raise ValueError("DI length and ADX smoothing must be positive")
        if self.pivot_left < 1 or self.pivot_right < 1:
            raise ValueError("ADX pivot left/right must be positive")
        self.symbol = str(params.get("symbol", "XAUUSDT")).upper()
        self.exchange = str(params.get("exchange", "binance"))
        self.capital = float(params.get("capital", 10_000.0))
        self.leverage = float(params.get("leverage", 10.0))
        self.position_fraction = float(params.get("position_fraction", 1.0))
        self.fee_pct = float(params.get("fee_pct", 0.00035))
        self.m15_warmup_bars = int(params.get("warmup_bars", 320))
        self.retain_bars = int(params.get("retain_bars", self.m15_warmup_bars))
        self.min_m15_bars = int(params.get("min_m15_bars", 80))
        self.timestamp_semantics = str(
            params.get("timestamp_semantics", "open")
        ).lower()
        if self.timestamp_semantics not in {"open", "close"}:
            raise ValueError("timestamp_semantics must be 'open' or 'close'")
        self._last_trend_dir: int = 0
        self._last_15m_open: int | None = None
        self._pending_15m_open: int | None = None
        self._positions: dict[str, dict[str, Any]] = self._load_positions()

    # ------------------------------------------------------------------ runner API

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        return ["kline:15m"]

    def get_required_channels_instance(self) -> list[str]:
        return self.__class__.get_required_channels(self.params)

    def get_warmup_symbols(self) -> list[str]:
        return [self.symbol]

    def get_warmup_tfs(self) -> list[str]:
        return ["15m"]

    def get_warmup_bars(self, tf: str) -> int:
        return self.m15_warmup_bars

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
        return False

    async def scan(self) -> None:
        if self._pending_15m_open is not None:
            await self._scan_15m(self._pending_15m_open)
            self._pending_15m_open = None
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
            len(series.closes) < self.min_m15_bars + 1
            or not series.times
            or series.times[-1] != candle_open_ms
        ):
            return

        # A new timestamp means the preceding 15m candle is now closed. All
        # indicator decisions use only that closed prefix; the new candle open
        # is the executable reference price for the resulting signal.
        closed = CandleSeries(
            series.opens[:-1],
            series.highs[:-1],
            series.lows[:-1],
            series.closes[:-1],
            series.times[:-1],
        )
        directions = compute_supertrend_directions(
            closed.highs,
            closed.lows,
            closed.closes,
            factor=self.factor,
            period=self.atr_period,
        )
        _, _, adx = compute_dmi_adx(
            closed.highs,
            closed.lows,
            closed.closes,
            di_length=self.di_length,
            adx_smoothing=self.adx_smoothing,
        )
        d_curr = directions[-1]
        execution_price = series.opens[-1]

        if self._last_15m_open is None:
            self._last_15m_open = candle_open_ms
            position = self._existing_position()
            if position is not None:
                position["last_trend_dir"] = (
                    UPTREND if position["side"] == "LONG" else DOWNTREND
                )
                self._last_trend_dir = int(position["last_trend_dir"])
            else:
                self._last_trend_dir = d_curr
            return
        self._last_15m_open = candle_open_ms
        position = self._existing_position()

        trend_changed = d_curr != self._last_trend_dir
        if trend_changed:
            self._last_trend_dir = d_curr
            if position is not None:
                position["previous_adx_peak"] = None
                position["tp_triggered"] = False
                is_opposite = (
                    position["side"] == "LONG" and d_curr == DOWNTREND
                ) or (position["side"] == "SHORT" and d_curr == UPTREND)
                if is_opposite:
                    await self._close_position(
                        position, "TREND_FLIP", execution_price, candle_open_ms
                    )
                    position = None
            side = "SHORT" if d_curr == DOWNTREND else "LONG"
            await self._open_if_allowed(side, execution_price, candle_open_ms)
            return

        if position is None:
            return

        confirmation_index = len(adx) - 1
        pivot = confirmed_pivot_high(
            adx, confirmation_index, self.pivot_left, self.pivot_right
        )
        if pivot is None or bool(position.get("tp_triggered", False)):
            return

        pivot_index, peak = pivot
        # Pine checks that the actual pivot bar and its later confirmation bar
        # belong to the same Supertrend cycle.
        if any(direction != d_curr for direction in directions[pivot_index:]):
            return
        previous = position.get("previous_adx_peak")
        if previous is not None and peak < float(previous) - self.min_peak_drop:
            position["tp_triggered"] = True
            await self._close_position(
                position, "ADX_LOWER_PEAK_TP", execution_price, candle_open_ms
            )
            return
        position["previous_adx_peak"] = peak

    # ------------------------------------------------------------------ series

    def _series(self, tf: str, bars: int) -> CandleSeries:
        snapshot = self.ctx.cache.snapshot(self.symbol, tf, bars)
        times = tuple(
            self._bar_open_ms(t, M15_MS) for t in snapshot.times
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
        self, side: str, entry: float, candle_open_ms: int
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
                f"{self.alpha_id}|{self.version}|adx|{candle_open_ms}|{side}",
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
            "previous_adx_peak": None,
            "tp_triggered": False,
        }
        metadata = {
            "strategy_type": self.strategy_type,
            "di_length": self.di_length,
            "adx_smoothing": self.adx_smoothing,
            "pivot_left": self.pivot_left,
            "pivot_right": self.pivot_right,
            "min_peak_drop": self.min_peak_drop,
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
                    "source": self.signal_source,
                    "ref_is_executable": True,
                    "signal_candle_open_ms": candle_open_ms,
                },
                sort_keys=True,
            ),
        )
        self._positions.pop(position["position_id"], None)



class SupertrendAdxEma13TpRunnerStrategy(_SupertrendAdxPivotRunnerBase):
    """ADX 4/2 lower-peak weakness with adverse EMA13 close confirmation."""

    strategy_type = "supertrend_adx_4_2_ema13_tp"
    signal_source = "supertrend_adx_4_2_ema13_tp"

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        self.ema_period = int(params.get("ema_period", 13))
        if self.ema_period < 1:
            raise ValueError("ema_period must be positive")

    @staticmethod
    def _ema(values: tuple[float, ...], period: int) -> list[float]:
        if not values:
            return []
        alpha = 2.0 / (period + 1.0)
        result = [values[0]]
        for value in values[1:]:
            result.append(alpha * value + (1.0 - alpha) * result[-1])
        return result

    async def _scan_15m(self, candle_open_ms: int) -> None:
        series = self._series("15m", self.get_retain_bars("15m"))
        if (
            len(series.closes) < self.min_m15_bars + 1
            or not series.times
            or series.times[-1] != candle_open_ms
        ):
            return
        closed = CandleSeries(
            series.opens[:-1], series.highs[:-1], series.lows[:-1],
            series.closes[:-1], series.times[:-1],
        )
        directions = compute_supertrend_directions(
            closed.highs, closed.lows, closed.closes,
            factor=self.factor, period=self.atr_period,
        )
        _, _, adx = compute_dmi_adx(
            closed.highs, closed.lows, closed.closes,
            self.di_length, self.adx_smoothing,
        )
        ema = self._ema(closed.closes, self.ema_period)
        d_curr = directions[-1]
        close = closed.closes[-1]
        execution_price = series.opens[-1]

        if self._last_15m_open is None:
            self._last_15m_open = candle_open_ms
            position = self._existing_position()
            self._last_trend_dir = (
                UPTREND if position and position["side"] == "LONG"
                else DOWNTREND if position else d_curr
            )
            return

        self._last_15m_open = candle_open_ms
        position = self._existing_position()
        if d_curr != self._last_trend_dir:
            self._last_trend_dir = d_curr
            if position is not None:
                is_opposite = (
                    position["side"] == "LONG" and d_curr == DOWNTREND
                ) or (position["side"] == "SHORT" and d_curr == UPTREND)
                if is_opposite:
                    await self._close_position(
                        position, "TREND_FLIP", execution_price, candle_open_ms
                    )
                    position = None
            side = "SHORT" if d_curr == DOWNTREND else "LONG"
            await self._open_if_allowed(side, execution_price, candle_open_ms)
            return

        if position is None:
            return
        pivot = confirmed_pivot_high(
            adx, len(adx) - 1, self.pivot_left, self.pivot_right
        )
        if pivot is not None:
            pivot_index, peak = pivot
            if not any(direction != d_curr for direction in directions[pivot_index:]):
                previous = position.get("previous_adx_peak")
                if previous is not None and peak < float(previous) - self.min_peak_drop:
                    position["adx_weakness_armed"] = True
                position["previous_adx_peak"] = peak

        if bool(position.get("adx_weakness_armed", False)):
            confirmed = (
                close <= ema[-1] if position["side"] == "LONG" else close >= ema[-1]
            )
            if confirmed:
                await self._close_position(
                    position, "ADX_4_2_EMA13_TP", execution_price, candle_open_ms
                )

