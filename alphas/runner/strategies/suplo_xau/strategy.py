"""Native runner implementation of the two suplo-xau alpha presets.

Reference: docs/suplo-xau-5.py and docs/suplo-xau-10.py (Supertrend baseline +
dynamic trailing take-profit).

The reference module is a bar-based state machine over a plain OHLC dataframe
(no timeframe is hardcoded in the source).  The sibling V2 modules
(alpha_supertrend_trailing_*_v2.py) explicitly state "Tính tín hiệu Supertrend
trên nến 15m", so this V1 family runs on raw 15m candles: enter on a Supertrend
direction flip at the candle close, trail a take-profit level that tracks the
peak gain from entry, and stand in cash after a trailing exit until the *next*
direction flip.

Direction convention (from the reference): ``1`` = downtrend, ``-1`` = uptrend.
A flip to ``1`` opens SHORT, a flip to ``-1`` opens LONG.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from runner.strategy.base import Strategy

M15_MS = 15 * 60 * 1000
POSITION_NAMESPACE = uuid.UUID("5f2e4b8a-3c1d-4f6e-9a2b-7d8e1c4f6a9b")

# Reference direction constants.
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
    """Supertrend direction array (1 = downtrend, -1 = uptrend).

    Byte-for-byte port of the reference ``compute_supertrend`` direction logic
    (the runner only needs direction, not the supertrend line value).
    """
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


@dataclass(frozen=True)
class StateStep:
    """Outcome of stepping the reference state machine over one candle."""

    position: int  # 1 = LONG, -1 = SHORT, 0 = CASH after this candle
    entry_price: float
    peak_gain: float
    target_exit_price: float | None
    last_trend_dir: int
    trailing_exit_hit: bool
    flip_exit_hit: bool
    flip_open: bool
    side: str | None


def step_state_machine(
    *,
    current_pos: int,
    entry_p: float,
    peak_p: float,
    last_trend_dir: int,
    high: float,
    low: float,
    close: float,
    direction: int,
    retrace_pct: float,
) -> StateStep:
    """One iteration of the reference ``generate_alpha_signals_trailing_*`` loop.

    Ordering is identical to the reference: trailing-exit check first, then the
    Supertrend flip check on the same candle.
    """
    trailing_exit_hit = False
    target_exit_price: float | None = None

    # 1. Dynamic trailing TP check for the running position.
    if current_pos != 0:
        curr_gain = (high - entry_p) if current_pos == 1 else (entry_p - low)
        if curr_gain > peak_p:
            peak_p = curr_gain
        if peak_p > 0:
            allowed_gain = peak_p * (1.0 - retrace_pct)
            target_exit_price = (
                entry_p + allowed_gain if current_pos == 1 else entry_p - allowed_gain
            )
            hit_exit = (
                low <= target_exit_price
                if current_pos == 1
                else high >= target_exit_price
            )
            if hit_exit:
                current_pos = 0
                peak_p = 0.0
                trailing_exit_hit = True

    # 2. Supertrend flip check -> open (or switch) position.
    flip_exit_hit = False
    flip_open = False
    side: str | None = None
    if direction != last_trend_dir:
        last_trend_dir = direction
        if direction != 0:
            if current_pos != 0:
                flip_exit_hit = True  # old position implicitly closed at this close
            current_pos = -1 if direction == DOWNTREND else 1
            entry_p = close
            peak_p = 0.0
            flip_open = True
            side = "SHORT" if current_pos == -1 else "LONG"

    return StateStep(
        position=current_pos,
        entry_price=entry_p,
        peak_gain=peak_p,
        target_exit_price=target_exit_price,
        last_trend_dir=last_trend_dir,
        trailing_exit_hit=trailing_exit_hit,
        flip_exit_hit=flip_exit_hit,
        flip_open=flip_open,
        side=side,
    )


class SuploXauRunnerStrategy(Strategy):
    """One parameterized strategy class for suplo-xau-5 and suplo-xau-10.

    ``preset`` selects the trailing retrace: 5 -> 5% retrace, 10 -> 10% retrace.
    Runs on raw 15m candles, matching the reference module semantics.
    """

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        self.preset = int(params.get("preset", 5))
        if self.preset not in {5, 10}:
            raise ValueError("suplo_xau preset must be 5 or 10")
        self.retrace_pct = float(params.get("retrace_pct", self.preset / 100.0))
        self.factor = float(params.get("factor", 3.0))
        self.atr_period = int(params.get("atr_period", 10))
        self.symbol = str(params.get("symbol", "XAUUSDT")).upper()
        self.exchange = str(params.get("exchange", "binance"))
        self.capital = float(params.get("capital", 10_000.0))
        self.leverage = float(params.get("leverage", 10.0))
        self.position_fraction = float(params.get("position_fraction", 1.0))
        self.fee_pct = float(params.get("fee_pct", 0.0005))
        self.m15_warmup_bars = int(params.get("warmup_bars", 320))
        self.retain_bars = int(params.get("retain_bars", self.m15_warmup_bars))
        self.min_bars = int(params.get("min_bars", 80))
        self.timestamp_semantics = str(
            params.get("timestamp_semantics", "open")
        ).lower()
        if self.timestamp_semantics not in {"open", "close"}:
            raise ValueError("timestamp_semantics must be 'open' or 'close'")
        self.timezone = ZoneInfo(
            str(params.get("session_timezone", "America/New_York"))
        )
        self.trade_weekends = bool(params.get("trade_weekends", True))
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
        """Normalize Unix seconds/milliseconds to milliseconds."""
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
        if (
            kind != "kline"
            or symbol != self.symbol
            or tf != "15m"
            or not self.ctx.state.ready
        ):
            return False
        latest = self.ctx.cache.get_latest_timestamp(self.symbol, "15m")
        if latest is None:
            return False
        open_ms = self._bar_open_ms(latest, M15_MS)
        if self._last_15m_open is not None and open_ms <= self._last_15m_open:
            return False
        self._pending_15m_open = open_ms
        return True

    def _is_trade_time(self, candle_open: int) -> bool:
        if self.trade_weekends:
            return True
        instant = datetime.fromtimestamp((candle_open + M15_MS) / 1000, tz=timezone.utc)
        return instant.weekday() < 5

    async def scan(self) -> None:
        if self._pending_15m_open is None:
            return
        series = self._series()
        if (
            len(series.closes) < self.min_bars
            or not series.times
            or series.times[-1] != self._pending_15m_open
        ):
            return
        if self._last_15m_open is None:
            # Fresh (re)start: adopt the current trend and any open position from
            # the worker snapshot without replaying history -- wait for the next
            # real flip, never open from historical candles.
            self._last_15m_open = self._pending_15m_open
            position = self._existing_position()
            if position is not None:
                position["last_trend_dir"] = (
                    UPTREND if position["side"] == "LONG" else DOWNTREND
                )
                self._last_trend_dir = int(position["last_trend_dir"])
            else:
                self._last_trend_dir = directions_last(series)
            self._pending_15m_open = None
            self._persist_positions()
            return
        self._last_15m_open = self._pending_15m_open
        self._pending_15m_open = None
        await self._manage_on_bar(series)
        if not self.ctx.can_open_trades() or not self._is_trade_time(series.times[-1]):
            self._persist_positions()
            return
        await self._evaluate_entries(series)
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
            owner_preset = value.get("preset") or (
                runtime.get("preset") if isinstance(runtime, dict) else None
            )
            if (
                str(value.get("symbol", "")).upper() == self.symbol
                and str(owner_alpha or "") == self.alpha_id
                and str(owner_version or "") == self.version
                and str(owner_preset or "") == str(self.preset)
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

    # ------------------------------------------------------------------ 15m series

    def _series(self) -> CandleSeries:
        snapshot = self.ctx.cache.snapshot(
            self.symbol, "15m", self.get_retain_bars("15m")
        )
        times = tuple(self._bar_open_ms(t, M15_MS) for t in snapshot.times)
        return CandleSeries(
            tuple(snapshot.opens),
            tuple(snapshot.highs),
            tuple(snapshot.lows),
            tuple(snapshot.closes),
            times,
        )

    # ------------------------------------------------------------- signal management

    def _existing_position(self) -> dict[str, Any] | None:
        if not self._positions:
            return None
        return next(iter(self._positions.values()))

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
                f"{self.alpha_id}|{self.version}|{self.preset}|{candle_open_ms}|{side}",
            )
        )
        position = {
            "position_id": position_id,
            "symbol": self.symbol,
            "alpha_id": self.alpha_id,
            "version": self.version,
            "preset": self.preset,
            "side": side,
            "entry": entry,
            "qty": qty,
            "tp": None,
            "sl": None,
            "entry_candle_open_ms": candle_open_ms,
            "last_trend_dir": UPTREND if side == "LONG" else DOWNTREND,
            "peak_gain": 0.0,
        }
        metadata = {
            "preset": self.preset,
            "retrace_pct": self.retrace_pct,
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
                    "preset": self.preset,
                    "source": "suplo_xau_bar",
                    "signal_candle_open_ms": candle_open_ms,
                },
                sort_keys=True,
            ),
        )
        self._positions.pop(position["position_id"], None)

    async def _manage_on_bar(self, series: CandleSeries) -> None:
        """Trailing take-profit management for the open position."""
        position = self._existing_position()
        if position is None:
            return
        side = str(position["side"])
        entry = float(position["entry"])
        high, low = series.highs[-1], series.lows[-1]
        curr_gain = (high - entry) if side == "LONG" else (entry - low)
        peak_p = max(float(position.get("peak_gain", 0.0)), curr_gain)
        if peak_p <= 0:
            position["peak_gain"] = 0.0
            return
        allowed_gain = peak_p * (1.0 - self.retrace_pct)
        target_exit = entry + allowed_gain if side == "LONG" else entry - allowed_gain
        hit_exit = (low <= target_exit) if side == "LONG" else (high >= target_exit)
        if hit_exit:
            await self._close_position(
                position,
                reason="TRAILING_TP",
                exit_price=target_exit,
                candle_open_ms=series.times[-1],
            )
            return
        # Persist the updated peak + trailing level so a restart keeps trailing.
        position["peak_gain"] = peak_p
        position["tp"] = target_exit
        current_emitted = position.get("tp_emitted")
        if current_emitted is None or abs(float(current_emitted) - target_exit) > 1e-9:
            await self.ctx.emit_signal(
                "MODIFY",
                position_id=position["position_id"],
                symbol=self.symbol,
                tp=target_exit,
                metadata=json.dumps(
                    {"preset": self.preset, "reason": "TRAILING_TP_UPDATE"},
                    sort_keys=True,
                ),
            )
            position["tp_emitted"] = target_exit

    async def _evaluate_entries(self, series: CandleSeries) -> None:
        """Step the reference state machine over the newest candle only.

        The running state is reconstructed from the persisted open position
        (entry/peak/side) and the in-memory last trend direction, so exactly one
        step per scan -- matching the reference loop's per-candle semantics.
        """
        directions = compute_supertrend_directions(
            series.highs,
            series.lows,
            series.closes,
            factor=self.factor,
            period=self.atr_period,
        )
        n = len(series.closes)
        if n == 0:
            return
        for i in range(n - 1, n):
            step = step_state_machine(
                current_pos=_pos_from_position(self._existing_position()),
                entry_p=_entry_from_position(self._existing_position()),
                peak_p=_peak_from_position(self._existing_position()),
                last_trend_dir=self._last_trend_dir,
                high=series.highs[i],
                low=series.lows[i],
                close=series.closes[i],
                direction=directions[i],
                retrace_pct=self.retrace_pct,
            )
            self._last_trend_dir = step.last_trend_dir
            position = self._existing_position()
            if step.trailing_exit_hit and position is not None:
                await self._close_position(
                    position,
                    reason="TRAILING_TP",
                    exit_price=step.target_exit_price,
                    candle_open_ms=series.times[i],
                )
            elif step.flip_exit_hit and position is not None:
                await self._close_position(
                    position,
                    reason="TREND_FLIP",
                    exit_price=step.entry_price,
                    candle_open_ms=series.times[i],
                )
            if step.flip_open and step.side is not None:
                await self._open_if_allowed(
                    step.side, step.entry_price, series.times[i]
                )


def directions_last(series: CandleSeries) -> int:
    """Current Supertrend direction of the last completed 15m candle."""
    directions = compute_supertrend_directions(series.highs, series.lows, series.closes)
    return directions[-1] if directions else 0


def _pos_from_position(position: dict[str, Any] | None) -> int:
    if position is None:
        return 0
    return 1 if position["side"] == "LONG" else -1


def _entry_from_position(position: dict[str, Any] | None) -> float:
    return float(position["entry"]) if position is not None else 0.0


def _peak_from_position(position: dict[str, Any] | None) -> float:
    return float(position.get("peak_gain", 0.0)) if position is not None else 0.0
