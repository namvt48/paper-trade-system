"""Smooth adaptive chandelier runner strategy.

Causal port of the smooth-chan research (docs/Downloads/alpha/backtest_smooth_chan.py,
exit_lab.py compute_supertrend_multi, h1_strategy_lab.py, supertrend_h1_gold.pine):

* Supertrend(factor, 10) on closed candles drives LONG/SHORT entries on flips.
* A smooth chandelier stop trails the position: the chandelier multiplier drips
  linearly from ``chan_start`` down to ``chan_min`` as the position's peak profit
  (from the watermark) rises toward ``scale_pct``.
* The chandelier uses the ENTRY ATR (fixed at trade open), so tightening the
  multiplier genuinely shrinks the stop distance.
* Optional breakeven (``be_pct``) moves the stop to entry after a set gain.
* Optional ``flip_only`` mode disables chandelier/breakeven exits (pure
  Supertrend flip exit), matching the exit_lab A0 baseline.

The effective timeframe is either ``1h`` natively or ``3h`` built deterministically
from adjacent ``1h`` bars (MDS has no 3h channel). A completed 3h candle is never
evaluated partially.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from typing import Any

from runner.strategy.base import Strategy

H1_MS = 60 * 60 * 1000
H3_MS = 3 * H1_MS
M15_MS = 15 * 60 * 1000
POSITION_NAMESPACE = uuid.UUID("c9a7e2f0-4b6d-4f3e-8a1c-5d9b7e2f4a83")

DOWNTREND = 1  # supertrend below price -> SHORT
UPTREND = -1  # supertrend above price -> LONG


@dataclass(frozen=True)
class CandleSeries:
    opens: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    closes: tuple[float, ...]
    times: tuple[int, ...]


def _true_ranges(
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    closes: tuple[float, ...],
) -> list[float]:
    if not closes:
        return []
    # Reference (exit_lab) computes TR against prev_close via shift(1), so the
    # first true range is NaN. ranges[0] = NaN mirrors that.
    ranges: list[float] = [math.nan]
    for index in range(1, len(closes)):
        ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    return ranges


def _wilder_atr(values: list[float], period: int) -> list[float]:
    """Wilder RMA ATR with an SMA seed, matching ``exit_lab.compute_supertrend_multi``.

    The reference seeds ``atr[period-1] = tr.iloc[:period].mean()``; pandas
    ``.mean()`` skips NaN, so the seed is the mean of the first ``period``
    non-NaN true ranges (bars 1..period-1), not ``values[0]``.
    """
    n = len(values)
    result = [math.nan] * n
    if period <= 0:
        raise ValueError("period must be positive")
    if n < period:
        return result
    seed_values = [v for v in values[:period] if math.isfinite(v)]
    result[period - 1] = (
        sum(seed_values) / len(seed_values) if seed_values else math.nan
    )
    for i in range(period, n):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return result


def compute_supertrend_multi(
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    closes: tuple[float, ...],
    factor: float = 3.0,
    atr_period: int = 10,
) -> tuple[list[int], list[float]]:
    """Return ``(direction, atr)`` matching the reference ``compute_supertrend_multi``.

    ``direction`` is ``1`` (downtrend -> SHORT) or ``-1`` (uptrend -> LONG).
    """
    n = len(closes)
    atr = _wilder_atr(_true_ranges(highs, lows, closes), atr_period)
    hl2 = [(highs[i] + lows[i]) / 2.0 for i in range(n)]
    bu = [
        hl2[i] + factor * atr[i] if math.isfinite(atr[i]) else math.nan
        for i in range(n)
    ]
    bl = [
        hl2[i] - factor * atr[i] if math.isfinite(atr[i]) else math.nan
        for i in range(n)
    ]

    upper = [0.0] * n
    lower = [0.0] * n
    supertrend = [0.0] * n
    direction = [0] * n

    for i in range(n):
        if i < atr_period - 1:
            continue
        if i == atr_period - 1:
            upper[i] = bu[i]
            lower[i] = bl[i]
            direction[i] = 1 if closes[i] <= bu[i] else -1
            supertrend[i] = upper[i] if direction[i] == 1 else lower[i]
            continue
        lower[i] = (
            bl[i]
            if (bl[i] > lower[i - 1] or closes[i - 1] < lower[i - 1])
            else lower[i - 1]
        )
        upper[i] = (
            bu[i]
            if (bu[i] < upper[i - 1] or closes[i - 1] > upper[i - 1])
            else upper[i - 1]
        )
        prev_st = supertrend[i - 1]
        if prev_st == upper[i - 1]:
            if closes[i] > upper[i]:
                direction[i], supertrend[i] = -1, lower[i]
            else:
                direction[i], supertrend[i] = 1, upper[i]
        else:
            if closes[i] < lower[i]:
                direction[i], supertrend[i] = 1, upper[i]
            else:
                direction[i], supertrend[i] = -1, lower[i]
    return direction, atr


class SmoothChanRunnerStrategy(Strategy):
    """Parametrized smooth adaptive chandelier (15m, 1h native or 3h-from-1h)."""

    strategy_type = "smooth_chan"
    signal_source = "smooth_chan"

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        self.tf = str(params.get("tf", "1h")).lower()
        if self.tf not in {"15m", "1h", "3h"}:
            raise ValueError("tf must be '15m', '1h', or '3h'")
        self.factor = float(params.get("factor", 2.5))
        self.atr_period = int(params.get("atr_period", 10))
        self.chan_start = float(params.get("chan_start", 3.0))
        self.chan_min = float(params.get("chan_min", 2.0))
        self.scale_pct = float(params.get("scale_pct", 0.15))
        self.be_pct = float(params.get("be_pct", 0.02))
        self.sl_k = float(params.get("sl_k", 0.0))
        self.flip_only = bool(params.get("flip_only", False))
        if self.chan_start < self.chan_min:
            raise ValueError("chan_start must be >= chan_min")
        if self.scale_pct < 0:
            raise ValueError("scale_pct must be >= 0")
        if self.atr_period < 1:
            raise ValueError("atr_period must be positive")
        self.symbol = str(params.get("symbol", "PAXGUSDT")).upper()
        self.exchange = str(params.get("exchange", "binance"))
        self.capital = float(params.get("capital", 10_000.0))
        self.leverage = float(params.get("leverage", 10.0))
        self.position_fraction = float(params.get("position_fraction", 1.0))
        self.fee_pct = float(params.get("fee_pct", 0.0005))
        self.warmup_bars = int(params.get("warmup_bars", 0) or 0) or (
            1440 if self.tf == "15m" else 320 if self.tf == "1h" else 960
        )
        self.retain_bars = int(params.get("retain_bars", 0) or 0) or self.warmup_bars
        self.min_bars = int(params.get("min_bars", 0) or 0) or (
            80 if self.tf == "1h" else 30
        )
        self.timestamp_semantics = str(
            params.get("timestamp_semantics", "open")
        ).lower()
        if self.timestamp_semantics not in {"open", "close"}:
            raise ValueError("timestamp_semantics must be 'open' or 'close'")
        # Native MDS channel backing this effective tf: 15m is native; 1h and 3h
        # (built from 1h) both subscribe to 1h.
        self.source_tf = "15m" if self.tf == "15m" else "1h"
        self.source_ms = M15_MS if self.tf == "15m" else H1_MS
        self._last_dir: int = 0
        self._last_eff_open: int | None = None
        self._pending_eff_open: int | None = None
        self._positions: dict[str, dict[str, Any]] = self._load_positions()

    # ------------------------------------------------------------------ runner API

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        tf = str(params.get("tf", "1h")).lower()
        # 15m requests the 15m channel directly; 1h/3h request 1h.
        return ["kline:15m"] if tf == "15m" else ["kline:1h"]

    def get_required_channels_instance(self) -> list[str]:
        return self.__class__.get_required_channels(self.params)

    def get_warmup_symbols(self) -> list[str]:
        return [self.symbol]

    def get_warmup_tfs(self) -> list[str]:
        return [self.source_tf]

    def get_warmup_bars(self, tf: str) -> int:
        return self.warmup_bars

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
        if (
            kind != "kline"
            or symbol != self.symbol
            or tf != self.source_tf
            or not self.ctx.state.ready
        ):
            return False
        latest = self.ctx.cache.get_latest_timestamp(self.symbol, self.source_tf)
        if latest is None:
            return False
        latest_open = self._bar_open_ms(latest, self.source_ms)
        if self.tf == "3h":
            # A new 3h bucket completes only when its third 1h bar closes.
            if latest_open % H3_MS != 2 * H1_MS:
                return False
            eff_open = latest_open - 2 * H1_MS
        else:
            eff_open = latest_open
        if self._last_eff_open is not None and eff_open <= self._last_eff_open:
            return False
        self._pending_eff_open = eff_open
        return True

    async def scan(self) -> None:
        if self._pending_eff_open is not None:
            await self._scan_eff(self._pending_eff_open)
            self._pending_eff_open = None
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

    # ------------------------------------------------------------------ series

    def _eff_series(self, bars: int) -> CandleSeries:
        """Return the effective (15m/1h or completed-3h) candle series."""
        snapshot = self.ctx.cache.snapshot(self.symbol, self.source_tf, bars)
        opens = tuple(snapshot.opens)
        highs = tuple(snapshot.highs)
        lows = tuple(snapshot.lows)
        closes = tuple(snapshot.closes)
        times = tuple(self._bar_open_ms(t, self.source_ms) for t in snapshot.times)
        if self.tf in {"15m", "1h"}:
            return CandleSeries(opens, highs, lows, closes, times)
        # Build completed 3h bars from adjacent 1h bars.
        by_open: dict[int, tuple[float, float, float, float]] = {}
        for t, o, h, l, c in zip(times, opens, highs, lows, closes):
            by_open[t] = (o, h, l, c)
        rows: list[tuple[int, float, float, float, float]] = []
        for start in sorted(by_open):
            if (
                start % H3_MS != 0
                or start + H1_MS not in by_open
                or start + 2 * H1_MS not in by_open
            ):
                continue
            first, second, third = (
                by_open[start],
                by_open[start + H1_MS],
                by_open[start + 2 * H1_MS],
            )
            rows.append(
                (
                    start,
                    first[0],
                    max(first[1], second[1], third[1]),
                    min(first[2], second[2], third[2]),
                    third[3],
                )
            )
        return CandleSeries(
            tuple(r[1] for r in rows),
            tuple(r[2] for r in rows),
            tuple(r[3] for r in rows),
            tuple(r[4] for r in rows),
            tuple(r[0] for r in rows),
        )

    # --------------------------------------------------------------- scan

    async def _scan_eff(self, eff_open_ms: int) -> None:
        series = self._eff_series(self.get_retain_bars(self.source_tf))
        if (
            len(series.closes) < self.min_bars + 1
            or not series.times
            or series.times[-1] != eff_open_ms
        ):
            return
        # A new timestamp means the preceding effective candle is now closed. All
        # decisions use that closed prefix; the new candle open is the executable
        # reference price for the resulting signal.
        closed = CandleSeries(
            series.opens[:-1],
            series.highs[:-1],
            series.lows[:-1],
            series.closes[:-1],
            series.times[:-1],
        )
        direction, atr = compute_supertrend_multi(
            closed.highs, closed.lows, closed.closes, self.factor, self.atr_period
        )
        d_curr = direction[-1]
        execution_price = series.opens[-1]

        if self._last_eff_open is None:
            self._last_eff_open = eff_open_ms
            position = self._existing_position()
            self._last_dir = (
                UPTREND
                if position and position["side"] == "LONG"
                else DOWNTREND
                if position
                else (d_curr or UPTREND)
            )
            return
        self._last_eff_open = eff_open_ms
        position = self._existing_position()

        if position is not None:
            exited = await self._manage_exits(position, closed, d_curr, execution_price)
            position = None if exited else self._existing_position()

        if d_curr != self._last_dir:
            self._last_dir = d_curr
            if position is not None:
                is_opposite = (position["side"] == "LONG" and d_curr == DOWNTREND) or (
                    position["side"] == "SHORT" and d_curr == UPTREND
                )
                if is_opposite:
                    await self._close_position(
                        position, "TREND_FLIP", execution_price, eff_open_ms
                    )
                    position = None
            side = "SHORT" if d_curr == DOWNTREND else "LONG"
            await self._open_if_allowed(side, execution_price, eff_open_ms, atr[-1])
            return

    async def _manage_exits(
        self,
        position: dict[str, Any],
        closed: CandleSeries,
        d_curr: int,
        execution_price: float,
    ) -> bool:
        """Smooth chandelier + breakeven exits on the closed prefix. Returns whether closed."""
        side = str(position["side"])
        entry = float(position["entry"])
        entry_atr = float(position.get("entry_atr") or 0.0)
        wm = float(position.get("wm") or entry)
        high = closed.highs[-1]
        low = closed.lows[-1]

        if side == "LONG":
            if high > wm:
                wm = high
            profit_pct = (wm - entry) / entry if entry else 0.0
        else:
            if low < wm:
                wm = low
            profit_pct = (entry - wm) / entry if entry else 0.0
        position["wm"] = wm

        ratio = (
            min(1.0, max(0.0, profit_pct / self.scale_pct))
            if self.scale_pct > 0
            else 0.0
        )
        chan_mult = self.chan_start - (self.chan_start - self.chan_min) * ratio

        if self.sl_k > 0 and entry_atr > 0:
            sl_level = (
                entry - self.sl_k * entry_atr
                if side == "LONG"
                else entry + self.sl_k * entry_atr
            )
            if (side == "LONG" and low <= sl_level) or (
                side == "SHORT" and high >= sl_level
            ):
                await self._close_position(
                    position, "SL", execution_price, closed.times[-1]
                )
                return True

        if not self.flip_only and entry_atr > 0:
            if side == "LONG":
                cs = wm - chan_mult * entry_atr
                if cs > entry and low <= cs:
                    await self._close_position(
                        position, "CH", execution_price, closed.times[-1]
                    )
                    return True
            else:
                cs = wm + chan_mult * entry_atr
                if cs < entry and high >= cs:
                    await self._close_position(
                        position, "CH", execution_price, closed.times[-1]
                    )
                    return True

        if self.be_pct > 0 and not self.flip_only:
            if side == "LONG":
                if high > entry * (1 + self.be_pct):
                    position["be"] = True
                if position.get("be") and low <= entry:
                    await self._close_position(
                        position, "BE", execution_price, closed.times[-1]
                    )
                    return True
            else:
                if low < entry * (1 - self.be_pct):
                    position["be"] = True
                if position.get("be") and high >= entry:
                    await self._close_position(
                        position, "BE", execution_price, closed.times[-1]
                    )
                    return True
        return False

    # ------------------------------------------------------------- signal management

    async def _open_if_allowed(
        self, side: str, entry: float, eff_open_ms: int, entry_atr: float
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
                f"{self.alpha_id}|{self.version}|{self.tf}|{eff_open_ms}|{side}",
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
            "entry_eff_open_ms": eff_open_ms,
            "entry_atr": entry_atr,
            "wm": entry,
            "be": False,
            "last_dir": UPTREND if side == "LONG" else DOWNTREND,
        }
        metadata = {
            "strategy_type": self.strategy_type,
            "tf": self.tf,
            "factor": self.factor,
            "chan_start": self.chan_start,
            "chan_min": self.chan_min,
            "scale_pct": self.scale_pct,
            "be_pct": self.be_pct,
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
            tf=self.tf,
            signal_candle_open_ms=eff_open_ms,
            metadata=json.dumps(metadata, sort_keys=True),
        )
        self._positions[position_id] = position

    async def _close_position(
        self,
        position: dict[str, Any],
        reason: str,
        exit_price: float | None,
        eff_open_ms: int,
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
                    "signal_candle_open_ms": eff_open_ms,
                },
                sort_keys=True,
            ),
        )
        self._positions.pop(position["position_id"], None)
