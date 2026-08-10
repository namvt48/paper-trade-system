"""Native runner implementation of the six XAU M30 alpha presets.

The market-data service publishes 15m and 4h bars.  This strategy deterministically
builds completed M30 bars from adjacent M15 bars, so it never evaluates a partial
M30 candle or requires a new MDS timeframe.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Literal
from zoneinfo import ZoneInfo

from runner.strategy.base import Strategy

Side = Literal["LONG", "SHORT"]
M15_MS = 15 * 60 * 1000
M30_MS = 30 * 60 * 1000
H4_MS = 4 * 60 * 60 * 1000
POSITION_NAMESPACE = uuid.UUID("b70b3d13-4e36-4a1e-a3bb-4188ad9c3700")


@dataclass(frozen=True)
class CandleSeries:
    opens: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    closes: tuple[float, ...]
    times: tuple[int, ...]


@dataclass(frozen=True)
class Preset:
    number: int
    max_positions: int
    rr: float
    pure_atr_stop: bool
    session_gated: bool
    macro_gated: bool
    long_only: bool = False
    session_start: int = 7
    session_end: int = 18


PRESETS = {
    4: Preset(4, 3, 2.0, False, True, True),
    5: Preset(5, 1, 1.8, False, True, True),
    6: Preset(6, 1, 2.0, False, True, True),
    10: Preset(10, 2, 2.5, True, False, False, True),
    11: Preset(11, 1, 2.2, True, True, True, session_end=15),
    12: Preset(12, 1, 2.6, True, True, True, session_end=15),
}


def _ema(values: tuple[float, ...], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _rsi(values: tuple[float, ...], period: int = 14) -> list[float]:
    if len(values) < 2:
        return [50.0] * len(values)
    gains = [0.0]
    losses = [0.0]
    for previous, current in zip(values, values[1:]):
        delta = current - previous
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    alpha = 1.0 / period
    avg_gain = gains[0]
    avg_loss = losses[0]
    out: list[float] = []
    for gain, loss in zip(gains, losses):
        avg_gain = alpha * gain + (1.0 - alpha) * avg_gain
        avg_loss = alpha * loss + (1.0 - alpha) * avg_loss
        out.append(
            100.0
            if avg_loss == 0 and avg_gain > 0
            else 50.0
            if avg_loss == 0
            else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        )
    return out


def _atr(series: CandleSeries, period: int = 14) -> list[float]:
    if not series.closes:
        return []
    ranges = [series.highs[0] - series.lows[0]]
    for index in range(1, len(series.closes)):
        ranges.append(
            max(
                series.highs[index] - series.lows[index],
                abs(series.highs[index] - series.closes[index - 1]),
                abs(series.lows[index] - series.closes[index - 1]),
            )
        )
    alpha = 1.0 / period
    result = [ranges[0]]
    for value in ranges[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


class XauM30RunnerStrategy(Strategy):
    """One parameterized strategy class for Alpha 4/5/6/10/11/12."""

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        preset_number = int(params.get("preset", 0))
        if preset_number not in PRESETS:
            raise ValueError("xau_m30 preset must be one of 4, 5, 6, 10, 11, 12")
        self.preset = PRESETS[preset_number]
        self.symbol = str(params.get("symbol", "XAUUSDT")).upper()
        self.exchange = str(params.get("exchange", "binance"))
        self.capital = float(params.get("capital", 10_000.0))
        self.risk_pct = float(params.get("risk_pct", 0.01))
        self.fee_pct = float(params.get("fee_pct", 0.0005))
        self.leverage = float(params.get("leverage", 10.0))
        self.m15_warmup_bars = int(params.get("warmup_bars", 320))
        self.h4_warmup_bars = int(params.get("h4_warmup_bars", 80))
        self.retain_bars = int(params.get("retain_bars", self.m15_warmup_bars))
        self.timestamp_semantics = str(
            params.get("timestamp_semantics", "open")
        ).lower()
        if self.timestamp_semantics not in {"open", "close"}:
            raise ValueError("timestamp_semantics must be 'open' or 'close'")
        self.timezone = ZoneInfo(
            str(params.get("session_timezone", "America/New_York"))
        )
        # Session behavior follows PRESETS; Alpha 4/5/6/11/12 are session-gated,
        # while Alpha 10 trades without a session gate.
        self.trade_weekends = bool(
            params.get("trade_weekends", not self.preset.session_gated)
        )
        self._last_m30_open: int | None = None
        self._pending_m30_open: int | None = None
        self._positions: dict[str, dict[str, Any]] = self._load_positions()

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        preset_number = int(params.get("preset", 0))
        channels = ["kline:15m"]
        if PRESETS.get(preset_number, Preset(0, 0, 0, False, False, False)).macro_gated:
            channels.append("kline:4h")
        return channels

    def get_required_channels_instance(self) -> list[str]:
        return self.__class__.get_required_channels(self.params)

    def get_warmup_symbols(self) -> list[str]:
        return [self.symbol]

    def get_warmup_tfs(self) -> list[str]:
        return ["15m", "4h"] if self.preset.macro_gated else ["15m"]

    def get_warmup_bars(self, tf: str) -> int:
        return self.h4_warmup_bars if tf == "4h" else self.m15_warmup_bars

    def get_retain_bars(self, tf: str) -> int:
        return max(self.get_warmup_bars(tf), self.retain_bars)

    async def _shared_panel_bundle(self):
        return None

    @staticmethod
    def _timestamp_ms(value: int) -> int:
        """Normalize Unix seconds/milliseconds to milliseconds."""
        value = int(value)
        return value * 1000 if abs(value) < 1_000_000_000_000 else value

    def _bar_open_ms(self, value: int, timeframe_ms: int) -> int:
        value_ms = self._timestamp_ms(value)
        if self.timestamp_semantics == "close":
            # Some APIs use the exact boundary, others use boundary - 1 ms.
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
        latest_open = self._bar_open_ms(latest, M15_MS)
        if latest_open % M30_MS != M15_MS:
            return False
        m30_open = latest_open - M15_MS
        if self._last_m30_open is not None and m30_open <= self._last_m30_open:
            return False
        self._pending_m30_open = m30_open
        return True

    async def scan(self) -> None:
        if self._pending_m30_open is None:
            return
        m30 = self._m30_series()
        if len(m30.closes) < 80 or m30.times[-1] != self._pending_m30_open:
            return
        h4 = self._completed_h4(m30.times[-1] + M30_MS)
        if self.preset.macro_gated and len(h4.closes) < 55:
            return
        self._last_m30_open = self._pending_m30_open
        self._pending_m30_open = None
        await self._manage_on_bar(m30)
        if not self.ctx.can_open_trades() or not self._is_trade_time(m30.times[-1]):
            self._persist_positions()
            return
        side = self._entry_side(m30, h4)
        if side is not None:
            await self._open_if_allowed(side, m30)
        self._persist_positions()

    async def manage_positions(self) -> None:
        self._sync_price_alerts()

    async def on_price_alert(self, symbol: str, price: float, side: str) -> None:
        if symbol != self.symbol:
            return
        for position_id, position in list(self._positions.items()):
            position_side = str(position["side"])
            hit_tp = (position_side == "LONG" and price >= float(position["tp"])) or (
                position_side == "SHORT" and price <= float(position["tp"])
            )
            hit_sl = (position_side == "LONG" and price <= float(position["sl"])) or (
                position_side == "SHORT" and price >= float(position["sl"])
            )
            if not (hit_tp or hit_sl):
                continue
            await self.ctx.emit_signal(
                "CLOSE",
                position_id=position_id,
                symbol=self.symbol,
                reason="TP_HIT" if hit_tp else "SL_HIT",
                exit_price=price,
                metadata=json.dumps(
                    {"ref_is_executable": True, "source": "xau_m30_price_alert"}
                ),
            )
            self._positions.pop(position_id, None)
        self._persist_positions()

    def _load_positions(self) -> dict[str, dict[str, Any]]:
        source = self.ctx.load_authoritative_positions()
        if source is None:
            source = self.ctx.load_positions()
        if not isinstance(source, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, value in source.items():
            runtime = value.get("strategy_runtime", {}) if isinstance(value, dict) else {}
            owner_alpha = value.get("alpha_id") if isinstance(value, dict) else None
            owner_version = value.get("version") if isinstance(value, dict) else None
            owner_preset = value.get("preset") if isinstance(value, dict) else None
            if isinstance(runtime, dict):
                owner_alpha = owner_alpha or runtime.get("alpha_id")
                owner_version = owner_version or runtime.get("version")
                owner_preset = owner_preset or runtime.get("preset")
            if (
                isinstance(value, dict)
                and str(value.get("symbol", "")).upper() == self.symbol
                and str(owner_alpha or "") == self.alpha_id
                and str(owner_version or "") == self.version
                and str(owner_preset or "") == str(self.preset.number)
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

    def _m30_series(self) -> CandleSeries:
        snapshot = self.ctx.cache.snapshot(
            self.symbol, "15m", self.get_retain_bars("15m")
        )
        bars = {
            self._bar_open_ms(time, M15_MS): (op, high, low, close)
            for time, op, high, low, close in zip(
                snapshot.times,
                snapshot.opens,
                snapshot.highs,
                snapshot.lows,
                snapshot.closes,
            )
        }
        rows: list[tuple[int, float, float, float, float]] = []
        for start in sorted(bars):
            if start % M30_MS != 0 or start + M15_MS not in bars:
                continue
            first, second = bars[start], bars[start + M15_MS]
            rows.append(
                (
                    start,
                    first[0],
                    max(first[1], second[1]),
                    min(first[2], second[2]),
                    second[3],
                )
            )
        return CandleSeries(
            tuple(row[1] for row in rows),
            tuple(row[2] for row in rows),
            tuple(row[3] for row in rows),
            tuple(row[4] for row in rows),
            tuple(row[0] for row in rows),
        )

    def _completed_h4(self, m30_close: int) -> CandleSeries:
        snapshot = self.ctx.cache.snapshot(
            self.symbol, "4h", self.get_retain_bars("4h")
        )
        rows = [
            (self._bar_open_ms(time, H4_MS), op, high, low, close)
            for time, op, high, low, close in zip(
                snapshot.times,
                snapshot.opens,
                snapshot.highs,
                snapshot.lows,
                snapshot.closes,
            )
            if self._bar_open_ms(time, H4_MS) + H4_MS <= m30_close
        ]
        return CandleSeries(
            tuple(row[1] for row in rows),
            tuple(row[2] for row in rows),
            tuple(row[3] for row in rows),
            tuple(row[4] for row in rows),
            tuple(row[0] for row in rows),
        )

    def _is_trade_time(self, candle_open: int) -> bool:
        instant = datetime.fromtimestamp(
            (candle_open + M30_MS) / 1000, tz=timezone.utc
        ).astimezone(self.timezone)
        if not self.trade_weekends and instant.weekday() >= 5:
            return False
        return (
            not self.preset.session_gated
            or self.preset.session_start <= instant.hour <= self.preset.session_end
        )

    def _entry_side(self, series: CandleSeries, h4: CandleSeries) -> Side | None:
        ema9, ema21, ema50 = (
            _ema(series.closes, 9),
            _ema(series.closes, 21),
            _ema(series.closes, 50),
        )
        rsi, atr = _rsi(series.closes), _atr(series)
        i = len(series.closes) - 1
        if atr[i] <= 0:
            return None
        macro = self._macro_side(h4) if self.preset.macro_gated else None
        if self.preset.macro_gated and macro is None:
            # Reference logic (alpha_logic_bundle/backtest_engine.py:307-309,
            # alpha_11.py:44-51) requires an UNAMBIGUOUS H4 trend match
            # (h4_trend == 1 for BUY, == -1 for SELL) -- a neutral H4 blocks
            # BOTH sides. It is not "allow unless H4 disagrees".
            return None
        candidates: tuple[Side, ...] = (
            ("LONG",) if self.preset.long_only else ("LONG", "SHORT")
        )
        for side in candidates:
            if self.preset.macro_gated and side != macro:
                continue
            if self._matches(side, series, ema9, ema21, ema50, rsi, atr):
                return side
        return None

    @staticmethod
    def _macro_side(h4: CandleSeries) -> Side | None:
        if len(h4.closes) < 54:
            return None
        ema9, ema21, ema50 = (
            _ema(h4.closes, 9),
            _ema(h4.closes, 21),
            _ema(h4.closes, 50),
        )
        if (
            ema9[-1] > ema21[-1] > ema50[-1]
            and h4.closes[-1] > ema50[-1]
            and ema50[-1] > ema50[-4]
        ):
            return "LONG"
        if (
            ema9[-1] < ema21[-1] < ema50[-1]
            and h4.closes[-1] < ema50[-1]
            and ema50[-1] < ema50[-4]
        ):
            return "SHORT"
        return None

    def _matches(
        self,
        side: Side,
        s: CandleSeries,
        e9: list[float],
        e21: list[float],
        e50: list[float],
        rsi: list[float],
        atr: list[float],
    ) -> bool:
        i = len(s.closes) - 1
        bullish = side == "LONG"
        trend = (
            e9[i] > e21[i] > e50[i] and s.closes[i] > e50[i]
            if bullish
            else e9[i] < e21[i] < e50[i] and s.closes[i] < e50[i]
        )
        if self.preset.number in {5, 6}:
            trend = trend and (
                e21[i] > e21[i - 3] and e50[i] > e50[i - 3]
                if bullish
                else e21[i] < e21[i - 3] and e50[i] < e50[i - 3]
            )
        elif self.preset.number in {4, 6, 10}:
            trend = trend and (e50[i] > e50[i - 3] if bullish else e50[i] < e50[i - 3])
        if self.preset.number == 6:
            return trend and (
                s.closes[i] > s.highs[i - 1] and s.closes[i] > e9[i]
                if bullish
                else s.closes[i] < s.lows[i - 1] and s.closes[i] < e9[i]
            )
        if self.preset.number == 12:
            if len(s.closes) < 42:
                return False
            volatility = atr[i] / median(atr[i - 40 : i])
            breakout = (
                s.closes[i] > max(s.highs[i - 20 : i])
                if bullish
                else s.closes[i] < min(s.lows[i - 20 : i])
            )
            impulse = s.highs[i] - s.lows[i] >= 0.75 * atr[i]
            direction = (
                s.closes[i] > s.opens[i] if bullish else s.closes[i] < s.opens[i]
            )
            # Alpha 12 uses EMA21/50 (not the generic EMA9 alignment).
            trend12 = (
                e21[i] > e50[i] and e21[i] > e21[i - 3] and e50[i] > e50[i - 3]
                if bullish
                else e21[i] < e50[i] and e21[i] < e21[i - 3] and e50[i] < e50[i - 3]
            )
            return (
                trend12
                and 0.90 <= volatility <= 2.50
                and breakout
                and impulse
                and direction
            )
        if self.preset.number == 11:
            if len(s.closes) < 50:
                return False
            volatility = atr[i] / median(atr[i - 48 : i])
            touched = (
                s.lows[i] <= e21[i] or s.lows[i - 1] <= e21[i - 1]
                if bullish
                else s.highs[i] >= e21[i] or s.highs[i - 1] >= e21[i - 1]
            )
            reclaim = (
                s.closes[i] > e9[i] and s.closes[i] > s.highs[i - 1]
                if bullish
                else s.closes[i] < e9[i] and s.closes[i] < s.lows[i - 1]
            )
            rsi_ok = 42 <= rsi[i] <= 63 if bullish else 37 <= rsi[i] <= 58
            trend11 = (
                e9[i] > e21[i] > e50[i] and e21[i] > e21[i - 3] and e50[i] > e50[i - 3]
                if bullish
                else e9[i] < e21[i] < e50[i]
                and e21[i] < e21[i - 3]
                and e50[i] < e50[i - 3]
            )
            return (
                trend11
                and 0.70 <= volatility <= 2.20
                and touched
                and reclaim
                and rsi_ok
            )
        touched = (
            s.lows[i] <= max(e9[i], e21[i])
            if bullish
            else s.highs[i] >= min(e9[i], e21[i])
        )
        green = (
            s.closes[i] > s.opens[i] and s.closes[i] > e9[i]
            if bullish
            else s.closes[i] < s.opens[i] and s.closes[i] < e9[i]
        )
        if self.preset.number == 10:
            return trend and touched and green and 40 <= rsi[i] <= 60
        zone = (
            (35, 55)
            if self.preset.number == 5 and bullish
            else (45, 65)
            if self.preset.number == 5
            else (45, 55)
        )
        previous_in_zone = (
            zone[0] <= rsi[i - 1] <= zone[1] or zone[0] <= rsi[i - 2] <= zone[1]
        )
        turned = (
            rsi[i] > rsi[i - 1] if bullish else rsi[i] < rsi[i - 1]
        ) and previous_in_zone
        return trend and touched and green and turned

    async def _manage_on_bar(self, series: CandleSeries) -> None:
        if self.preset.number not in {4, 5, 6}:
            return
        e50 = _ema(series.closes, 50)[-1]
        price = series.closes[-1]
        for position_id, position in list(self._positions.items()):
            position_side = str(position["side"])
            reversal = (
                position_side == "LONG"
                and price < e50
                or position_side == "SHORT"
                and price > e50
            )
            if reversal:
                await self.ctx.emit_signal(
                    "CLOSE",
                    position_id=position_id,
                    symbol=self.symbol,
                    reason="EMA50_REVERSAL",
                    exit_price=price,
                    metadata=json.dumps(
                        {"ref_is_executable": False, "source": "xau_m30_bar"}
                    ),
                )
                self._positions.pop(position_id, None)
                continue
            entry, initial_sl = (
                float(position["entry"]),
                float(position.get("initial_sl", position["sl"])),
            )
            risk = abs(entry - initial_sl)
            reached = (
                price - entry >= 0.8 * risk
                if position_side == "LONG"
                else entry - price >= 0.8 * risk
            )
            if risk > 0 and reached and not position.get("breakeven"):
                await self.ctx.emit_signal(
                    "MODIFY",
                    position_id=position_id,
                    symbol=self.symbol,
                    sl=entry,
                    metadata=json.dumps({"reason": "BREAKEVEN_0_8R"}),
                )
                position["sl"] = entry
                position["breakeven"] = True

    async def _open_if_allowed(self, side: Side, series: CandleSeries) -> None:
        # max_positions is a per-strategy total-position cap, matching the
        # local backtest engine.  Do not count LONG and SHORT independently;
        # otherwise a max_positions=1 strategy could hedge with two positions.
        if len(self._positions) >= self.preset.max_positions:
            return
        same_side = [
            position
            for position in self._positions.values()
            if position.get("side") == side
        ]
        atr = _atr(series)[-1]
        entry = series.closes[-1]
        if any(
            abs(entry - float(position["entry"])) < 0.5 * atr for position in same_side
        ):
            return
        # Reference (alpha_logic_bundle/strategies/alpha_4.py
        # check_pyramiding_safety): blocks only a STRICTLY worse entry
        # (entry < existing for LONG, entry > existing for SHORT) -- an
        # entry at the exact same price as an existing leg is allowed, not
        # blocked.
        if self.preset.number == 4 and any(
            (
                entry < float(position["entry"])
                if side == "LONG"
                else entry > float(position["entry"])
            )
            for position in same_side
        ):
            return
        sl = self._stop_price(side, entry, series, atr)
        risk = abs(entry - sl)
        if risk <= 0:
            return
        qty = math.floor((self.capital * self.risk_pct / risk) * 1000) / 1000
        qty = min(qty, math.floor((self.capital * self.leverage / entry) * 1000) / 1000)
        if qty < 0.001:
            return
        tp = (
            entry + self.preset.rr * risk
            if side == "LONG"
            else entry - self.preset.rr * risk
        )
        position_id = str(
            uuid.uuid5(
                POSITION_NAMESPACE,
                f"{self.alpha_id}|{self.version}|{series.times[-1]}|{side}|{len(same_side)}",
            )
        )
        position = {
            "position_id": position_id,
            "symbol": self.symbol,
            "alpha_id": self.alpha_id,
            "version": self.version,
            "preset": self.preset.number,
            "side": side,
            "entry": entry,
            "qty": qty,
            "tp": tp,
            "sl": sl,
            "initial_sl": sl,
            "breakeven": False,
        }
        metadata = {
            "preset": self.preset.number,
            "strategy_runtime": position,
            "allow_duplicate_position": self.preset.max_positions > 1,
        }
        await self.ctx.emit_signal(
            "OPEN",
            position_id=position_id,
            symbol=self.symbol,
            side=side,
            entry=entry,
            qty=qty,
            tp=tp,
            sl=sl,
            leverage=self.leverage,
            exchange=self.exchange,
            fee_pct=self.fee_pct,
            tf="30m",
            signal_candle_open_ms=series.times[-1],
            metadata=json.dumps(metadata, sort_keys=True),
        )
        self._positions[position_id] = position

    def _stop_price(
        self, side: Side, entry: float, series: CandleSeries, atr: float
    ) -> float:
        if self.preset.pure_atr_stop:
            multiplier = {10: 1.2, 11: 1.6, 12: 1.35}[self.preset.number]
            return (
                entry - multiplier * atr if side == "LONG" else entry + multiplier * atr
            )
        low, high = min(series.lows[-11:]), max(series.highs[-11:])
        buffer = 1.5 * atr
        raw = (
            min(low, _ema(series.closes, 50)[-1]) - buffer
            if side == "LONG"
            else max(high, _ema(series.closes, 50)[-1]) + buffer
        )
        if abs(entry - raw) <= max(0.5, 0.5 * atr):
            raw = entry - 2.0 * atr if side == "LONG" else entry + 2.0 * atr
        return raw
