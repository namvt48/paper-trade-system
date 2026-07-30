"""Canonical JSONL serialization for deterministic recovery ledger entries."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from .domain import (
    AlphaId,
    CandleOpenMs,
    CloseLedgerEntry,
    LedgerEntry,
    OpenLedgerEntry,
    PositionId,
    Side,
    SignalId,
)


class LedgerKind(StrEnum):
    """Closed set of operations accepted by the candidate builder."""

    CLOSE = "CLOSE"
    OPEN = "OPEN"


class LedgerParseError(Exception):
    """A reviewed ledger contains an unsupported or malformed operation."""


def write_ledger(path: Path, entries: tuple[LedgerEntry, ...]) -> None:
    """Write stable JSONL suitable for review, hashing, and later promotion."""
    lines = [
        json.dumps(_to_row(entry), sort_keys=True, separators=(",", ":"))
        for entry in entries
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_ledger(path: Path) -> tuple[LedgerEntry, ...]:
    """Parse a reviewed JSONL ledger into closed typed variants."""
    entries: list[LedgerEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        try:
            kind = LedgerKind(row["kind"])
        except ValueError as exc:
            raise LedgerParseError(
                f"unsupported ledger kind: {row.get('kind')}"
            ) from exc
        match kind:
            case LedgerKind.CLOSE:
                entries.append(
                    CloseLedgerEntry(
                        alpha_id=AlphaId(row["alpha_id"]),
                        signal_id=SignalId(row["signal_id"]),
                        position_id=PositionId(row["position_id"]),
                        symbol=row["symbol"],
                        side=Side(row["side"]),
                        decision_price=float(row["decision_price"]),
                        candle_open_ms=CandleOpenMs(int(row["candle_open_ms"])),
                        event_at=datetime.fromisoformat(row["event_at"]),
                        fee_pct=float(row["fee_pct"]),
                    )
                )
            case LedgerKind.OPEN:
                entries.append(
                    OpenLedgerEntry(
                        alpha_id=AlphaId(row["alpha_id"]),
                        signal_id=SignalId(row["signal_id"]),
                        position_id=PositionId(row["position_id"]),
                        symbol=row["symbol"],
                        side=Side(row["side"]),
                        decision_price=float(row["decision_price"]),
                        qty=float(row["qty"]),
                        weight=float(row["weight"]),
                        strategy_leverage=float(row["strategy_leverage"]),
                        candle_open_ms=CandleOpenMs(int(row["candle_open_ms"])),
                        event_at=datetime.fromisoformat(row["event_at"]),
                        fee_pct=float(row["fee_pct"]),
                    )
                )
            case unreachable:
                assert_never(unreachable)
    return tuple(entries)


def _to_row(entry: LedgerEntry) -> dict[str, str | int | float]:
    """Flatten a ledger variant into canonical scalar JSON fields."""
    common: dict[str, str | int | float] = {
        "alpha_id": str(entry.alpha_id),
        "signal_id": str(entry.signal_id),
        "position_id": str(entry.position_id),
        "symbol": entry.symbol,
        "side": entry.side.value,
        "decision_price": entry.decision_price,
        "candle_open_ms": int(entry.candle_open_ms),
        "event_at": entry.event_at.isoformat(),
        "fee_pct": entry.fee_pct,
    }
    match entry:
        case CloseLedgerEntry():
            return {**common, "kind": "CLOSE"}
        case OpenLedgerEntry():
            return {
                **common,
                "kind": "OPEN",
                "qty": entry.qty,
                "weight": entry.weight,
                "strategy_leverage": entry.strategy_leverage,
            }
        case unreachable:
            assert_never(unreachable)
