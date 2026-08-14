"""Bollinger mean-reversion Z-score strategy (long + short flip) on 1d candles.

Reference spec (Bollinger Mean-Reversion, Z < -2):

    ma20 = close.rolling(20).mean()     # 20-period SMA
    sd20 = close.rolling(20).std()      # 20-period sample std dev
    lower = ma20 - 2 * sd20             # lower Bollinger band

Daily-close zones (evaluated on the closed 1d prefix; fills at next open):

    price < lower        -> zone "LONG"   (Z < -2, buy dips)
    price > ma20         -> zone "SHORT"  (price back above MA, sell/fade)
    lower <= price <= ma -> zone "BETWEEN" (no fresh signal)

Position transition on each new 1d bar (closed prefix determines the zone):

    no position + LONG    -> open LONG
    no position + SHORT   -> open SHORT
    no position + BETWEEN -> stay flat
    LONG + SHORT          -> close LONG, open SHORT  (flip)
    SHORT + LONG          -> close SHORT, open LONG  (flip)
    LONG + BETWEEN        -> hold LONG               (giu lenh)
    SHORT + BETWEEN       -> close SHORT to cash     (cash lenh)
    LONG + LONG / SHORT + SHORT -> hold

Runner plumbing, position sizing, persistence and signal execution mirror the
existing single-symbol runner strategies (e.g. supertrend_adx_tp).
"""

from __future__ import annotations

import json
import math
import statistics
import uuid
from dataclasses import dataclass
from typing import Any

from runner.strategy.base import Strategy

D1_MS = 24 * 60 * 60 * 1000
POSITION_NAMESPACE = uuid.UUID("9c4e1d2f-6b8a-4e5f-9c3d-2a7b8f1e5d4c")

ZONE_LONG = "LONG"
ZONE_SHORT = "SHORT"
ZONE_BETWEEN = "BETWEEN"


@dataclass(frozen=True)
class CandleSeries:
    opens: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    closes: tuple[float, ...]
    times: tuple[int, ...]


def _zscore(closes: tuple[float, ...], period: int = 20) -> float | None:
    """Z-score of the last close against the trailing ``period`` closes.

    Returns ``None`` when fewer than ``period`` closes are available. Z is
    computed as ``(last_close - mean) / sample_std``; a value below -2 means
    the last close is below ``mean - 2*std`` (the lower Bollinger band).
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
    """Return the daily-close zone (LONG/SHORT/BETWEEN) or None if not warmed."""
    if len(closes) < period:
        return None
    z = _zscore(closes, period)
    if z is None:
        return None
    if z < -2.0:
        return ZONE_LONG
    if z > 0.0:
        return ZONE_SHORT
    return ZONE_BETWEEN


class BollingerMeanRevRunnerStrategy(Strategy):
    """Long/short flip on 1d Bollinger lower-band Z-score mean reversion."""

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        self.bb_period = int(params.get("bb_period", 20))
        self.z_entry = float(params.get("z_entry", -2.0))
        if self.bb_period < 2:
            raise ValueError("bb_period must be >= 2")
        self.symbol = str(params.get("symbol", "PAXGUSDT")).upper()
        self.exchange = str(params.get("exchange", "binance"))
        self.capital = float(params.get("capital", 10_000.0))
        self.leverage = float(params.get("leverage", 10.0))
        self.position_fraction = float(params.get("position_fraction", 1.0))
        self.fee_pct = float(params.get("fee_pct", 0.00035))
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

    # ------------------------------------------------------------------ runner API

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

    # -------------------------------------------------------------------- 1d scan

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

    async def _scan_1d(self, candle_open_ms: int) -> None:
        series = self._series("1d", self.get_retain_bars("1d"))
        if (
            len(series.closes) < self.min_d1_bars + 1
            or not series.times
            or series.times[-1] != candle_open_ms
        ):
            return

        # A new timestamp means the preceding 1d candle is now closed. All
        # zone decisions use only that closed prefix; the new candle open is
        # the executable reference price for the resulting signal.
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
        pos_side = position["side"] if position is not None else None

        # No position -> open on a fresh zone signal; BETWEEN stays flat.
        if position is None:
            if zone in (ZONE_LONG, ZONE_SHORT):
                await self._open_if_allowed(zone, execution_price, candle_open_ms)
            return

        # Position exists -> reconcile against the current zone.
        if pos_side == zone:
            return  # hold same-side
        if zone == ZONE_BETWEEN:
            # LONG held in the middle; SHORT is flushed to cash.
            if pos_side == ZONE_SHORT:
                await self._close_position(
                    position, "ZONE_BETWEEN_CASH", execution_price, candle_open_ms
                )
            return
        # zone is the OPPOSITE extreme -> flip (close old, open new).
        await self._close_position(
            position, "ZONE_FLIP", execution_price, candle_open_ms
        )
        await self._open_if_allowed(zone, execution_price, candle_open_ms)

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
                f"{self.alpha_id}|{self.version}|boll|{candle_open_ms}|{side}",
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
            "z_entry": self.z_entry,
            "bb_period": self.bb_period,
        }
        metadata = {
            "strategy_type": "bollinger_meanrev",
            "bb_period": self.bb_period,
            "z_entry": self.z_entry,
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
                    "source": "bollinger_meanrev",
                    "ref_is_executable": True,
                    "signal_candle_open_ms": candle_open_ms,
                },
                sort_keys=True,
            ),
        )
        self._positions.pop(position["position_id"], None)
