"""Recalculate affected equity rows inside a copied snapshot database."""

from __future__ import annotations

import bisect
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .configuration import RecoveryAlphaConfig
from .domain import AlphaId, LedgerEntry, Side


@dataclass(frozen=True, slots=True)
class PositionInterval:
    """One position lifecycle used for historical mark-to-market."""

    alpha_id: AlphaId
    symbol: str
    side: Side
    entry_price: float
    qty: float
    fee_pct: float
    opened_at: datetime
    closed_at: datetime | None


class EquityBuildError(Exception):
    """Candidate equity cannot be rebuilt without fabricating a price."""


class PriceArchive:
    """Immutable last-completed-15m price lookup for captured symbols."""

    def __init__(self, market_root: Path) -> None:
        self._times: dict[str, list[int]] = {}
        self._prices: dict[str, list[float]] = {}
        for path in (market_root / "equity-15m").glob("*.json"):
            rows = json.loads(path.read_text(encoding="utf-8"))
            self._times[path.stem] = [int(row["open_time"]) for row in rows]
            self._prices[path.stem] = [float(row["close"]) for row in rows]

    def at_or_before(self, symbol: str, timestamp: datetime) -> float:
        """Return the latest captured close, raising when coverage is absent."""
        times = self._times.get(symbol, [])
        target_ms = int((timestamp - timedelta(minutes=15)).timestamp() * 1000)
        index = bisect.bisect_right(times, target_ms) - 1
        if index < 0:
            raise EquityBuildError(
                f"equity price missing for {symbol} at {timestamp.isoformat()}"
            )
        return self._prices[symbol][index]


def build_equity_candidate(
    baseline_equity: Path,
    candidate_equity: Path,
    candidate_main: Path,
    market_root: Path,
    configs: tuple[RecoveryAlphaConfig, ...],
    ledger: tuple[LedgerEntry, ...],
) -> None:
    """Copy equity DB and rewrite affected rows from the earliest event onward."""
    if candidate_equity.exists():
        raise EquityBuildError(f"candidate already exists: {candidate_equity}")
    shutil.copy2(baseline_equity, candidate_equity)
    affected = {entry.alpha_id for entry in ledger}
    if not affected:
        return
    start = min(entry.event_at for entry in ledger)
    capitals = {config.alpha_id: config.capital for config in configs}
    intervals, realized = _load_lifecycles(candidate_main, affected)
    prices = PriceArchive(market_root)
    with sqlite3.connect(candidate_equity) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("BEGIN IMMEDIATE")
        timestamps = [
            datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            for row in conn.execute(
                "SELECT DISTINCT timestamp FROM equity_snapshots WHERE timestamp >= ? ORDER BY timestamp",
                (start.isoformat(),),
            )
        ]
        for timestamp in timestamps:
            for alpha_id in affected:
                real = sum(
                    pnl
                    for closed_at, pnl in realized.get(alpha_id, ())
                    if closed_at <= timestamp
                )
                unreal = sum(
                    _position_pnl(
                        position, prices.at_or_before(position.symbol, timestamp)
                    )
                    for position in intervals.get(alpha_id, ())
                    if position.opened_at <= timestamp
                    and (position.closed_at is None or position.closed_at > timestamp)
                )
                balance = capitals.get(alpha_id, 10_000.0) + real + unreal
                conn.execute(
                    """UPDATE equity_snapshots
                       SET balance = ?, unrealized_pnl = ?, realized_pnl = ?
                       WHERE alpha_id = ? AND timestamp = ?""",
                    (balance, unreal, real, str(alpha_id), timestamp.isoformat()),
                )
            total = conn.execute(
                "SELECT COALESCE(SUM(balance), 0) FROM equity_snapshots WHERE timestamp = ? AND alpha_id != '__TOTAL__'",
                (timestamp.isoformat(),),
            ).fetchone()[0]
            total_unreal = conn.execute(
                "SELECT COALESCE(SUM(unrealized_pnl), 0) FROM equity_snapshots WHERE timestamp = ? AND alpha_id != '__TOTAL__'",
                (timestamp.isoformat(),),
            ).fetchone()[0]
            total_real = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM equity_snapshots WHERE timestamp = ? AND alpha_id != '__TOTAL__'",
                (timestamp.isoformat(),),
            ).fetchone()[0]
            conn.execute(
                """UPDATE equity_snapshots
                   SET balance = ?, unrealized_pnl = ?, realized_pnl = ?
                   WHERE alpha_id = '__TOTAL__' AND timestamp = ?""",
                (total, total_unreal, total_real, timestamp.isoformat()),
            )
        conn.commit()


def _load_lifecycles(
    candidate_main: Path,
    affected: set[AlphaId],
) -> tuple[
    dict[AlphaId, tuple[PositionInterval, ...]],
    dict[AlphaId, tuple[tuple[datetime, float], ...]],
]:
    """Load closed and currently open lifecycles from the candidate main DB."""
    intervals: dict[AlphaId, list[PositionInterval]] = {alpha: [] for alpha in affected}
    realized: dict[AlphaId, list[tuple[datetime, float]]] = {
        alpha: [] for alpha in affected
    }
    with sqlite3.connect(f"file:{candidate_main.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in affected)
        params = tuple(str(alpha) for alpha in affected)
        for row in conn.execute(
            f"SELECT * FROM trades WHERE alpha_id IN ({placeholders})", params
        ):
            alpha_id = AlphaId(row["alpha_id"])
            denominator = (
                float(row["entry_price"]) + float(row["exit_price"])
            ) * float(row["qty"])
            fee_pct = float(row["fee"] or 0.0) / denominator if denominator else 0.0
            closed_at = _timestamp(row["closed_at"])
            intervals[alpha_id].append(_interval(row, fee_pct, closed_at))
            realized[alpha_id].append((closed_at, float(row["pnl"] or 0.0)))
        for row in conn.execute(
            f"SELECT * FROM positions WHERE alpha_id IN ({placeholders})", params
        ):
            alpha_id = AlphaId(row["alpha_id"])
            intervals[alpha_id].append(
                _interval(row, float(row["fee_pct"] or 0.0), None)
            )
    return (
        {alpha: tuple(rows) for alpha, rows in intervals.items()},
        {alpha: tuple(rows) for alpha, rows in realized.items()},
    )


def _interval(
    row: sqlite3.Row, fee_pct: float, closed_at: datetime | None
) -> PositionInterval:
    """Normalize a trade or open-position row into one lifecycle."""
    return PositionInterval(
        alpha_id=AlphaId(row["alpha_id"]),
        symbol=row["symbol"],
        side=Side(row["side"]),
        entry_price=float(row["entry_price"]),
        qty=float(row["qty"]),
        fee_pct=fee_pct,
        opened_at=_timestamp(row["opened_at"]),
        closed_at=closed_at,
    )


def _position_pnl(position: PositionInterval, current_price: float) -> float:
    """Mirror the worker equity collector's net unrealized PnL formula."""
    direction = 1.0 if position.side is Side.LONG else -1.0
    gross = (current_price - position.entry_price) * position.qty * direction
    fee = (position.entry_price + current_price) * position.qty * position.fee_pct
    return gross - fee


def _timestamp(raw: str) -> datetime:
    """Parse the ISO timestamps stored by the worker."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
