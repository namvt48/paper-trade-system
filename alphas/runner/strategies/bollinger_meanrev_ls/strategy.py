"""Bollinger Mean-Reversion Z-score LONG+SHORT flip strategy (1d candles).

Faithful port of the Pine ``Boll MeanRev L+S Flip`` reference spec:

    length = 20                      # BB period (SMA / stdev window)
    basis  = sma(close, length)
    dev    = stdev(close, length)    # sample stdev, matches ta.stdev()
    zScore = (close - basis) / dev
    prevZ  = z-score of the PREVIOUS bar close (nz-guarded to 0)

Zone is derived from the just-closed bar's ``prevZ`` (evaluated on the closed
1d prefix, fills at the next bar open):

    prevZ < -2.0            -> BUY zone    (z==1)
    prevZ >  0.0            -> SELL zone   (z==-1)
    -2.0 <= prevZ <= 0.0    -> BETWEEN     (z==0)

Position transition on each new 1d bar (closed prefix determines the zone):

    flat + BUY     -> open LONG
    flat + SELL    -> open SHORT
    LONG + SELL    -> close LONG (FLIP) + open SHORT
    SHORT + BUY    -> close SHORT (FLIP) + open LONG
    SHORT + BETWEEN-> close SHORT (CASH)
    LONG + BETWEEN -> hold LONG
    bar year < start_year and in a position -> close all (OOR)

Runner plumbing, position sizing, persistence and signal execution mirror the
existing single-symbol runner strategies (bollinger_meanrev / supertrend_adx_tp).
"""

from __future__ import annotations

import json
import math
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from runner.strategy.base import Strategy

D1_MS = 24 * 60 * 60 * 1000
POSITION_NAMESPACE = uuid.UUID("7a1e8c3f-2b6d-4e9a-8f5c-1d3a6b9c2e7f")

ZONE_BUY = "BUY"
ZONE_SELL = "SELL"
ZONE_BETWEEN = "BETWEEN"


@dataclass(frozen=True)
class CandleSeries:
    opens: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    closes: tuple[float, ...]
    times: tuple[int, ...]


def _zscore(closes: tuple[float, ...], period: int = 20) -> float | None:
    """Z-score of the last close over a trailing window (sample stdev).

    Matches Pine ``(close - sma(close, length)) / stdev(close, length)`` where
    ``stdev`` is the sample standard deviation (``len - 1`` denominator).
    """
    if len(closes) < period or period < 2:
        return None
    window = list(closes[-period:])
    last = window[-1]
    mean = statistics.fmean(window)
    if len(window) == 1:
        return 0.0
    sample_var = sum((v - mean) ** 2 for v in window) / (len(window) - 1)
    std = math.sqrt(sample_var)
    if std == 0.0:
        return 0.0
    return (last - mean) / std


def _zone_from_closes(closes: tuple[float, ...], period: int = 20) -> str | None:
    """Map the most recent closed-bar z-score to a BUY / SELL / BETWEEN zone.

    ``closes`` here is the CLOSED prefix (last close is the just-closed bar), so
    the returned zone corresponds to Pine ``prevZ``.
    """
    if len(closes) < period:
        return None
    z = _zscore(closes, period)
    if z is None:
        return None
    if z < -2.0:
        return ZONE_BUY
    if z > 0.0:
        return ZONE_SELL
    return ZONE_BETWEEN


class BollingerMeanRevLsRunnerStrategy(Strategy):
    """Long+short flip on 1d Bollinger lower/upper-band Z-score mean reversion."""

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        self.bb_period = int(params.get("bb_period", 20))
        self.z_entry_buy = float(params.get("z_entry_buy", -2.0))
        self.z_entry_sell = float(params.get("z_entry_sell", 0.0))
        if self.bb_period < 2:
            raise ValueError("bb_period must be >= 2")
        self.symbol = str(params.get("symbol", "PAXGUSDT")).upper()
        self.exchange = str(params.get("exchange", "binance"))
        self.capital = float(params.get("capital", 10_000.0))
        self.leverage = float(params.get("leverage", 10.0))
        self.position_fraction = float(params.get("position_fraction", 1.0))
        self.fee_pct = float(params.get("fee_pct", 0.00035))
        self.start_year = int(params.get("start_year", 2024))
        self.d1_warmup_bars = int(params.get("warmup_bars", 60))
        self.retain_bars = int(params.get("retain_bars", self.d1_warmup_bars))
        self.min_d1_bars = int(params.get("min_d1_bars", self.bb_period + 4))
        self.timestamp_semantics = str(
            params.get("timestamp_semantics", "open")
        ).lower()
        if self.timestamp_semantics not in {"open", "close"}:
            raise ValueError("timestamp_semantics must be 'open' or 'close'")
        self._last_1d_open: int | None = None
        self._pending_1d_open: int | None = None
        self._positions: dict[str, dict[str, Any]] = self._load_positions()

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        return ["kline:1d"]

    def get_required_channels_instance(self) -> list[str]:
        return self.__class__.get_required_channels(self.params)

    def get_warmup_symbols(self) -> list[str]:
        return [self.symbol]

    def get_warmup_tfs(self) -> list[str]:
        return ["1d"]

    def get_warmup_bars(self, tf: str) -> int:
        return self.d1_warmup_bars

    def get_retain_bars(self, tf: str) -> int:
        return max(self.get_warmup_bars(tf), self.retain_bars)

    async def _shared_panel_bundle(self):
        return None

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
        if tf == "1d":
            open_ms = self._bar_open_ms(latest, D1_MS)
            if self._last_1d_open is not None and open_ms <= self._last_1d_open:
                return False
            self._pending_1d_open = open_ms
            return True
        return False

    async def scan(self) -> None:
        if self._pending_1d_open is not None:
            await self._scan_1d(self._pending_1d_open)
            self._pending_1d_open = None
        self._persist_positions()

    async def manage_positions(self) -> None:
        self._sync_price_alerts()

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

    def _series(self, tf: str, bars: int) -> CandleSeries:
        snapshot = self.ctx.cache.snapshot(self.symbol, tf, bars)
        times = tuple(self._bar_open_ms(t, D1_MS) for t in snapshot.times)
        return CandleSeries(
            tuple(snapshot.opens),
            tuple(snapshot.highs),
            tuple(snapshot.lows),
            tuple(snapshot.closes),
            times,
        )

    @staticmethod
    def _bar_year(open_ms: int) -> int:
        return datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc).year

    async def _scan_1d(self, candle_open_ms: int) -> None:
        series = self._series("1d", self.get_retain_bars("1d"))
        if (
            len(series.closes) < self.min_d1_bars + 1
            or not series.times
            or series.times[-1] != candle_open_ms
        ):
            return

        closed = series.closes[:-1]
        execution_price = float(series.opens[-1])
        zone = _zone_from_closes(closed, self.bb_period)
        if zone is None:
            return

        if self._last_1d_open is None:
            self._last_1d_open = candle_open_ms
            return
        self._last_1d_open = candle_open_ms

        position = self._existing_position()

        # Out-of-range guard: mirror Pine's `if not inRange ... close_all("OOR")`.
        in_range = self._bar_year(candle_open_ms) >= self.start_year
        if not in_range:
            if position is not None:
                await self._close_position(
                    position, "OOR", execution_price, candle_open_ms
                )
            return

        # ---- flat + zone -> open raw position in that direction ----
        if position is None:
            if zone == ZONE_BUY:
                await self._open_position("LONG", execution_price, candle_open_ms)
            elif zone == ZONE_SELL:
                await self._open_position("SHORT", execution_price, candle_open_ms)
            return

        # ---- holding a position ----
        side = str(position.get("side", "")).upper()
        if side == "LONG":
            # LONG + SELL zone -> flip to SHORT
            if zone == ZONE_SELL:
                await self._close_position(
                    position, "FLIP", execution_price, candle_open_ms
                )
                await self._open_position("SHORT", execution_price, candle_open_ms)
            # LONG + BUY/BETWEEN -> hold
        elif side == "SHORT":
            # SHORT + BUY zone -> flip to LONG
            if zone == ZONE_BUY:
                await self._close_position(
                    position, "FLIP", execution_price, candle_open_ms
                )
                await self._open_position("LONG", execution_price, candle_open_ms)
            # SHORT + BETWEEN zone -> close to cash
            elif zone == ZONE_BETWEEN:
                await self._close_position(
                    position, "CASH", execution_price, candle_open_ms
                )
            # SHORT + SELL zone -> hold

    async def _open_position(
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
                f"{self.alpha_id}|{self.version}|boll_ls|{candle_open_ms}|{side}",
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
            "z_entry_buy": self.z_entry_buy,
            "z_entry_sell": self.z_entry_sell,
            "bb_period": self.bb_period,
        }
        metadata = {
            "strategy_type": "bollinger_meanrev_ls",
            "bb_period": self.bb_period,
            "z_entry_buy": self.z_entry_buy,
            "z_entry_sell": self.z_entry_sell,
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
            tf="1d",
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
                    "source": "bollinger_meanrev_ls",
                    "ref_is_executable": True,
                    "signal_candle_open_ms": candle_open_ms,
                },
                sort_keys=True,
            ),
        )
        self._positions.pop(position["position_id"], None)
