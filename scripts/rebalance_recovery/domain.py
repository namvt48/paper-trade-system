"""Typed recovery contracts shared by capture, replay, candidate, and promotion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

AlphaId = NewType("AlphaId", str)
CandleOpenMs = NewType("CandleOpenMs", int)
PositionId = NewType("PositionId", str)
SignalId = NewType("SignalId", str)


class Side(StrEnum):
    """Position direction persisted by the paper-trade worker."""

    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class AlphaSchedule:
    """Incident scope and cadence for one alpha."""

    alpha_id: AlphaId
    timeframe: str
    rebalance_bars: int
    activation_close_date: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryPoint:
    """One missing rebalance identified by its decision candle and event time."""

    alpha_id: AlphaId
    timeframe: str
    candle_open_ms: CandleOpenMs
    event_at: datetime


@dataclass(frozen=True, slots=True)
class CloseLedgerEntry:
    """A deterministic historical close to apply only to a candidate database."""

    alpha_id: AlphaId
    signal_id: SignalId
    position_id: PositionId
    symbol: str
    side: Side
    decision_price: float
    candle_open_ms: CandleOpenMs
    event_at: datetime
    fee_pct: float


@dataclass(frozen=True, slots=True)
class OpenLedgerEntry:
    """A deterministic historical open to apply only to a candidate database."""

    alpha_id: AlphaId
    signal_id: SignalId
    position_id: PositionId
    symbol: str
    side: Side
    decision_price: float
    qty: float
    weight: float
    strategy_leverage: float
    candle_open_ms: CandleOpenMs
    event_at: datetime
    fee_pct: float


LedgerEntry = CloseLedgerEntry | OpenLedgerEntry
