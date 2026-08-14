"""Native runner implementation of the Supertrend XAU V4 MFE ATR Distance strategy.

V4 integrates the MFE ATR Distance Trailing algorithm based on 15m Snapshot ATR:
  - Supertrend(3, 10) is computed on RAW 15m candles.
  - ATR(10) is snapshotted at 15m entry to define dynamic distance:
      MFE < 0.5 * ATR                   : No Trailing
      MFE >= 0.5 * ATR AND age >= 2 min : Distance = 0.35 * ATR
      MFE >= 1.0 * ATR                  : Distance = 0.30 * ATR
      MFE >= 1.5 * ATR                  : Distance = 0.25 * ATR
  - Trailing exit triggers ONLY when 1m CLOSE breaks active trailing level:
      Long Exit  : minute_close < active_trailing
      Short Exit : minute_close > active_trailing
  - Trailing Ratchet is strictly monotonic (never loosens backwards):
      Long  : T_t = max(T_{t-1}, Candidate_t)
      Short : T_t = min(T_{t-1}, Candidate_t)
  - Exit prices include reference slippage (0.0001).
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from runner.strategy.base import Strategy

M1_MS = 60 * 1000
M15_MS = 15 * 60 * 1000
POSITION_NAMESPACE = uuid.UUID("b2c3d4e5-f6a7-8b9c-0d1e-2f3a4b5c6d7e")

DOWNTREND = 1   # supertrend below price -> SHORT
UPTREND = -1   # supertrend above price -> LONG
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
    """Wilder's RMA ATR, matching ta.atr(10)."""
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
) -> tuple[list[int], list[float]]:
    """Supertrend direction array and ATR array."""
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
    return direction, atr


def get_mfe_atr_distance(mfe_profit: float, atr_val: float, position_age_min: int) -> Optional[float]:
    """Calculate Trailing Distance according to MFE and Position Age."""
    if atr_val <= 0 or mfe_profit < 0.5 * atr_val:
        return None
    if mfe_profit < 1.0 * atr_val:
        if position_age_min < 2:
            return None
        return 0.35 * atr_val
    elif mfe_profit < 1.5 * atr_val:
        return 0.30 * atr_val
    else:
        return 0.25 * atr_val


def process_long_minute_v4(
    minute_open: float,
    minute_high: float,
    minute_low: float,
    minute_close: float,
    entry_price: float,
    best_high: float,
    active_trailing: Optional[float],
    atr_val: float,
    position_age_min: int,
    slippage_pct: float = REF_SLIPPAGE,
) -> Tuple[bool, Optional[float], float, Optional[float]]:
    """Minute-by-minute Long processing (Exit ONLY when 1m CLOSE < active_trailing)."""
    if active_trailing is not None:
        if minute_close < active_trailing:
            exit_price = minute_close * (1.0 - slippage_pct)
            return True, exit_price, best_high, None

    updated_best_high = max(best_high, minute_high)
    mfe_profit = updated_best_high - entry_price

    distance = get_mfe_atr_distance(mfe_profit, atr_val, position_age_min)
    if distance is None:
        return False, None, updated_best_high, active_trailing

    candidate_trailing = updated_best_high - distance
    if active_trailing is None:
        next_trailing = candidate_trailing
    else:
        next_trailing = max(active_trailing, candidate_trailing)
    return False, None, updated_best_high, next_trailing


def process_short_minute_v4(
    minute_open: float,
    minute_high: float,
    minute_low: float,
    minute_close: float,
    entry_price: float,
    best_low: float,
    active_trailing: Optional[float],
    atr_val: float,
    position_age_min: int,
    slippage_pct: float = REF_SLIPPAGE,
) -> Tuple[bool, Optional[float], float, Optional[float]]:
    """Minute-by-minute Short processing (Exit ONLY when 1m CLOSE > active_trailing)."""
    if active_trailing is not None:
        if minute_close > active_trailing:
            exit_price = minute_close * (1.0 + slippage_pct)
            return True, exit_price, best_low, None

    updated_best_low = min(best_low, minute_low)
    mfe_profit = entry_price - updated_best_low

    distance = get_mfe_atr_distance(mfe_profit, atr_val, position_age_min)
    if distance is None:
        return False, None, updated_best_low, active_trailing

    candidate_trailing = updated_best_low + distance
    if active_trailing is None:
        next_trailing = candidate_trailing
    else:
        next_trailing = min(active_trailing, candidate_trailing)
    return False, None, updated_best_low, next_trailing


class SupertrendXauV4MfeAtrRunnerStrategy(Strategy):
    """Supertrend(3,10) on 15m entries + 1m MFE ATR Distance Trailing strategy."""

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
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
        directions, atrs = compute_supertrend_directions(
            series.highs,
            series.lows,
            series.closes,
            factor=self.factor,
            period=self.atr_period,
        )
        current_atr = atrs[-1] if atrs else 1.0
        d_curr = directions[-1]

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
        if position is not None:
            position["latest_atr"] = current_atr

        if d_curr == self._last_trend_dir:
            return

        self._last_trend_dir = d_curr
        close = series.closes[-1]
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
        await self._open_if_allowed(side, close, candle_open_ms, current_atr)

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
        snapshot_atr = float(position.get("entry_atr", 1.0))
        entry_ms = int(position.get("entry_candle_open_ms", candle_open_ms))
        position_age_min = max(0, int((candle_open_ms - entry_ms) / M1_MS))

        o, h, l, c = series.opens[-1], series.highs[-1], series.lows[-1], series.closes[-1]

        if side == "LONG":
            best = float(position.get("best_high", entry))
            active = position.get("active_trailing")
            active = float(active) if active is not None else None
            exited, exit_price, best_high, next_trailing = process_long_minute_v4(
                o, h, l, c, entry, best, active, snapshot_atr, position_age_min
            )
            position["best_high"] = best_high
        else:
            best = float(position.get("best_low", entry))
            active = position.get("active_trailing")
            active = float(active) if active is not None else None
            exited, exit_price, best_low, next_trailing = process_short_minute_v4(
                o, h, l, c, entry, best, active, snapshot_atr, position_age_min
            )
            position["best_low"] = best_low

        if exited:
            await self._close_position(
                position, "V4_MFE_ATR_TRAILING_CLOSE_EXIT", exit_price, candle_open_ms
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
        self, side: str, entry: float, candle_open_ms: int, current_atr: float
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
                f"{self.alpha_id}|{self.version}|v4mfe|{candle_open_ms}|{side}",
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
            "entry_atr": current_atr,
            "latest_atr": current_atr,
        }
        metadata = {
            "strategy_type": "v4_mfe_atr_distance_close_exit",
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
                    "source": "supertrend_xau_v4_mfe_atr",
                    "ref_is_executable": True,
                    "signal_candle_open_ms": candle_open_ms,
                },
                sort_keys=True,
            ),
        )
        self._positions.pop(position["position_id"], None)
