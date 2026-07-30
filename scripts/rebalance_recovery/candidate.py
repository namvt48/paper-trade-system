"""Materialize an approved recovery ledger into a copied SQLite candidate."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import assert_never

from .domain import CloseLedgerEntry, LedgerEntry, OpenLedgerEntry, Side


class CandidateBuildError(Exception):
    """Candidate construction failed without mutating the source database."""


def build_main_candidate(
    source_db: Path,
    candidate_db: Path,
    entries: tuple[LedgerEntry, ...],
    recovered_at: datetime,
    slippage_pct: float = 0.05,
) -> None:
    """Copy a read-only source DB and apply ledger entries in one transaction."""
    if source_db.resolve() == candidate_db.resolve():
        raise CandidateBuildError("source and candidate paths must differ")
    if candidate_db.exists():
        raise CandidateBuildError(f"candidate already exists: {candidate_db}")
    candidate_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_db, candidate_db)
    with sqlite3.connect(candidate_db) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        for entry in entries:
            match entry:
                case CloseLedgerEntry():
                    _apply_close(conn, entry, recovered_at, slippage_pct)
                case OpenLedgerEntry():
                    _apply_open(conn, entry, recovered_at, slippage_pct)
                case unreachable:
                    assert_never(unreachable)
        conn.commit()


def _apply_close(
    conn: sqlite3.Connection,
    entry: CloseLedgerEntry,
    recovered_at: datetime,
    slippage_pct: float,
) -> None:
    """Close one candidate position using the worker's fee and slippage formulas."""
    position = conn.execute(
        "SELECT * FROM positions WHERE position_id = ?", (str(entry.position_id),)
    ).fetchone()
    if position is None:
        raise CandidateBuildError(f"missing position: {entry.position_id}")
    if (
        position["alpha_id"] != str(entry.alpha_id)
        or position["symbol"] != entry.symbol
        or position["side"] != entry.side.value
    ):
        raise CandidateBuildError(f"position identity mismatch: {entry.position_id}")
    _insert_signal(conn, entry, recovered_at)
    exit_price = _fixed_fill(
        entry.decision_price, entry.side, slippage_pct, is_close=True
    )
    qty = float(position["qty"])
    entry_price = float(position["entry_price"])
    direction = _direction(entry.side)
    fee_pct = float(position["fee_pct"] or entry.fee_pct)
    fee = (entry_price + exit_price) * qty * fee_pct
    pnl = (exit_price - entry_price) * qty * direction - fee
    capital = entry_price * qty
    opened_at = datetime.fromisoformat(
        str(position["opened_at"]).replace("Z", "+00:00")
    )
    duration_hours = (entry.event_at - opened_at).total_seconds() / 3600.0
    metadata = _merge_close_metadata(
        str(position["metadata"] or "{}"), entry, exit_price
    )
    conn.execute(
        """INSERT INTO trades
           (trade_id, position_id, alpha_id, signal_id, symbol, side, entry_price,
            exit_price, qty, pnl, pnl_percent, leverage, tp, sl, reason,
            duration_hours, opened_at, closed_at, metadata, fee, exchange)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(entry.position_id),
            str(entry.position_id),
            str(entry.alpha_id),
            position["signal_id"],
            entry.symbol,
            entry.side.value,
            entry_price,
            exit_price,
            qty,
            pnl,
            (pnl / capital * 100.0) if capital else 0.0,
            position["leverage"],
            position["tp"],
            position["sl"],
            "REBALANCE",
            duration_hours,
            position["opened_at"],
            entry.event_at.isoformat(),
            metadata,
            fee,
            position["exchange"] or "binance",
        ),
    )
    conn.execute(
        "DELETE FROM positions WHERE position_id = ?", (str(entry.position_id),)
    )


def _apply_open(
    conn: sqlite3.Connection,
    entry: OpenLedgerEntry,
    recovered_at: datetime,
    slippage_pct: float,
) -> None:
    """Open one candidate position with deterministic recovery metadata."""
    _insert_signal(conn, entry, recovered_at)
    fill_price = _fixed_fill(
        entry.decision_price, entry.side, slippage_pct, is_close=False
    )
    metadata = json.dumps(
        {
            "decision_price": entry.decision_price,
            "recorded_fill_price": fill_price,
            "weight": entry.weight,
            "strategy_leverage": entry.strategy_leverage,
            "entry_candle_open_ms": int(entry.candle_open_ms),
            "execution": {"initial_source": "historical_fixed_pct"},
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    conn.execute(
        """INSERT INTO positions
           (position_id, alpha_id, signal_id, symbol, side, entry_price, qty,
            leverage, opened_at, metadata, exchange, fee_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'binance', ?)""",
        (
            str(entry.position_id),
            str(entry.alpha_id),
            str(entry.signal_id),
            entry.symbol,
            entry.side.value,
            fill_price,
            entry.qty,
            entry.event_at.isoformat(),
            metadata,
            entry.fee_pct,
        ),
    )


def _insert_signal(
    conn: sqlite3.Connection,
    entry: LedgerEntry,
    recovered_at: datetime,
) -> None:
    """Persist audit evidence with historical event time and real ingestion time."""
    signal_type = "CLOSE" if isinstance(entry, CloseLedgerEntry) else "OPEN"
    payload = {
        "type": signal_type,
        "alpha_id": str(entry.alpha_id),
        "signal_id": str(entry.signal_id),
        "symbol": entry.symbol,
        "position_id": str(entry.position_id),
        "timestamp": entry.event_at.isoformat(),
        "signal_candle_open_ms": str(int(entry.candle_open_ms)),
        "recovery": {
            "recovered_at": recovered_at.isoformat(),
            "source": "staged_replay",
        },
    }
    conn.execute(
        """INSERT INTO signals
           (signal_id, alpha_id, type, payload, received_at, processed, error)
           VALUES (?, ?, ?, ?, ?, 1, NULL)""",
        (
            str(entry.signal_id),
            str(entry.alpha_id),
            signal_type,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            recovered_at.isoformat(),
        ),
    )


def _fixed_fill(
    price: float, side: Side, slippage_pct: float, *, is_close: bool
) -> float:
    """Apply the worker's adverse fixed-percentage fallback fill."""
    slip = price * (slippage_pct / 1000.0)
    match side:
        case Side.LONG:
            return price - slip if is_close else price + slip
        case Side.SHORT:
            return price + slip if is_close else price - slip
        case unreachable:
            assert_never(unreachable)


def _direction(side: Side) -> float:
    """Map a typed side to the signed PnL multiplier."""
    match side:
        case Side.LONG:
            return 1.0
        case Side.SHORT:
            return -1.0
        case unreachable:
            assert_never(unreachable)


def _merge_close_metadata(raw: str, entry: CloseLedgerEntry, fill_price: float) -> str:
    """Preserve open metadata and nest deterministic close audit fields."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"open_raw": raw}
    close_metadata = {
        "decision_price": entry.decision_price,
        "recorded_fill_price": fill_price,
        "signal_candle_open_ms": int(entry.candle_open_ms),
        "execution": {"initial_source": "historical_fixed_pct"},
    }
    if isinstance(parsed, dict):
        return json.dumps(
            {**parsed, "close": close_metadata},
            separators=(",", ":"),
            sort_keys=True,
        )
    return json.dumps(
        {"open_raw": raw, "close": close_metadata},
        separators=(",", ":"),
        sort_keys=True,
    )
