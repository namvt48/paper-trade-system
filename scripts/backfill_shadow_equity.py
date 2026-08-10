#!/usr/bin/env python3
"""Repair flat historical shadow-sleeve equity rows from ledger events + MDS.

The alpha runner's persisted shadow NAV is canonical at each strategy
timeframe boundary. Older collectors repeated that NAV between boundaries,
which produced horizontal chart segments. This script preserves each
canonical boundary row, replays virtual position events, and marks the rows
between boundaries with the latest completed MDS candle.

Example:
    python scripts/backfill_shadow_equity.py \
      --trade-db data/paper-trade.db \
      --snapshot-db data/equity-snapshots.db \
      --mds-cache /root/market-data-service/historical_cache/binance \
      --before 2026-07-30T11:49:44+00:00
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


def parse_timestamp(raw: str) -> datetime:
    timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def timeframe_seconds(raw: str) -> int:
    value = raw.strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400}
    if len(value) < 2 or value[-1] not in units:
        raise ValueError(f"unsupported timeframe: {raw}")
    return int(value[:-1]) * units[value[-1]]


class PriceLookup(Protocol):
    def get_price(self, symbol: str, timestamp: datetime) -> float | None: ...


class MdsPriceLookup:
    """Look up the most recent fully completed MDS candle close."""

    def __init__(self, cache_dir: str, timeframe: str = "15m") -> None:
        self._directory = Path(cache_dir) / timeframe
        self._interval_ms = timeframe_seconds(timeframe) * 1000
        self._cache: dict[str, tuple[list[int], list[float]]] = {}

    def _load_symbol(self, symbol: str) -> tuple[list[int], list[float]]:
        cached = self._cache.get(symbol)
        if cached is not None:
            return cached

        symbol_dir = self._directory / symbol
        by_open_time: dict[int, float] = {}
        if symbol_dir.is_dir():
            for parquet_file in sorted(symbol_dir.glob("*.parquet")):
                try:
                    table = pq.read_table(
                        parquet_file,
                        columns=["open_time", "close"],
                    )
                    for open_time, close in zip(
                        table.column("open_time").to_pylist(),
                        table.column("close").to_pylist(),
                    ):
                        price = float(close)
                        if price > 0:
                            by_open_time[int(open_time)] = price
                except Exception:
                    logger.exception("failed reading %s", parquet_file)

        ordered = sorted(by_open_time.items())
        result = (
            [item[0] for item in ordered],
            [item[1] for item in ordered],
        )
        self._cache[symbol] = result
        return result

    def get_price(self, symbol: str, timestamp: datetime) -> float | None:
        times, prices = self._load_symbol(symbol)
        if not times:
            return None

        # A candle with open_time=T is only knowable after T + interval.
        completed_before_ms = int(timestamp.timestamp() * 1000) - self._interval_ms
        index = bisect.bisect_right(times, completed_before_ms) - 1
        return prices[index] if index >= 0 else None


@dataclass(frozen=True)
class VirtualPosition:
    position_id: str
    symbol: str
    side: str
    entry_price: float
    qty: float

    def pnl(self, price: float) -> float:
        direction = 1.0 if self.side == "LONG" else -1.0
        return (price - self.entry_price) * self.qty * direction


@dataclass(frozen=True)
class LedgerEvent:
    timestamp: datetime
    event_type: str
    payload: dict[str, object]


@dataclass(frozen=True)
class RepairResult:
    alpha_count: int
    scanned_rows: int
    changed_rows: int
    missing_price_rows: int


def _event_timeframe(alpha_id: str, events: list[LedgerEvent]) -> str:
    for event in events:
        raw = event.payload.get("timeframe")
        if isinstance(raw, str) and raw:
            return raw
    prefix = alpha_id.split("-", 1)[0]
    timeframe_seconds(prefix)
    return prefix


def _load_events(
    connection: sqlite3.Connection,
) -> dict[str, list[LedgerEvent]]:
    result: dict[str, list[LedgerEvent]] = {}
    rows = connection.execute(
        """
        SELECT alpha_id, event_type, payload
        FROM virtual_trade_events
        ORDER BY processed_at, event_id
        """
    )
    for alpha_id, event_type, raw_payload in rows:
        try:
            payload = json.loads(raw_payload)
            timestamp = parse_timestamp(str(payload["timestamp"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("skipping malformed virtual event for %s", alpha_id)
            continue
        result.setdefault(str(alpha_id), []).append(
            LedgerEvent(
                timestamp=timestamp,
                event_type=str(event_type),
                payload=payload,
            )
        )
    for events in result.values():
        events.sort(key=lambda event: event.timestamp)
    return result


def _apply_event(
    active: dict[str, VirtualPosition],
    event: LedgerEvent,
) -> None:
    payload = event.payload
    position_id = str(payload["position_id"])
    if event.event_type == "VIRTUAL_OPEN":
        active[position_id] = VirtualPosition(
            position_id=position_id,
            symbol=str(payload["symbol"]),
            side=str(payload["side"]),
            entry_price=float(payload["price"]),
            qty=float(payload["qty"]),
        )
    elif event.event_type == "VIRTUAL_CLOSE":
        active.pop(position_id, None)


def _prices_for_positions(
    positions: dict[str, VirtualPosition],
    price_lookup: PriceLookup,
    timestamp: datetime,
) -> dict[str, float] | None:
    prices: dict[str, float] = {}
    for position in positions.values():
        if position.symbol in prices:
            continue
        price = price_lookup.get_price(position.symbol, timestamp)
        if price is None:
            return None
        prices[position.symbol] = price
    return prices


def repair_shadow_equity(
    trade_db_path: str,
    snapshot_db_path: str,
    price_lookup: PriceLookup,
    before: str,
    *,
    dry_run: bool = False,
) -> RepairResult:
    cutoff = parse_timestamp(before)
    trade_connection = sqlite3.connect(trade_db_path)
    events_by_alpha = _load_events(trade_connection)
    trade_connection.close()

    snapshot_connection = sqlite3.connect(snapshot_db_path)
    snapshot_connection.row_factory = sqlite3.Row
    snapshot_connection.execute("PRAGMA busy_timeout=20000")
    snapshot_connection.execute("BEGIN IMMEDIATE")

    scanned_rows = 0
    changed_rows = 0
    missing_price_rows = 0
    repaired_alphas = 0

    try:
        for alpha_id, events in sorted(events_by_alpha.items()):
            if not alpha_id.endswith("-sleeve") or not events:
                continue
            rows = snapshot_connection.execute(
                """
                SELECT id, timestamp, balance, unrealized_pnl, realized_pnl
                FROM equity_snapshots
                WHERE alpha_id = ?
                  AND julianday(timestamp) < julianday(?)
                ORDER BY julianday(timestamp), id
                """,
                (alpha_id, cutoff.isoformat()),
            ).fetchall()
            if not rows:
                continue

            repaired_alphas += 1
            scanned_rows += len(rows)
            interval_sec = timeframe_seconds(_event_timeframe(alpha_id, events))
            active: dict[str, VirtualPosition] = {}
            event_index = 0
            previous_bucket: int | None = None
            anchor_balance: float | None = None
            anchor_positions: dict[str, VirtualPosition] = {}
            anchor_prices: dict[str, float] = {}

            for row in rows:
                timestamp = parse_timestamp(str(row["timestamp"]))
                events_applied = False
                while (
                    event_index < len(events)
                    and events[event_index].timestamp <= timestamp
                ):
                    _apply_event(active, events[event_index])
                    event_index += 1
                    events_applied = True

                bucket = int(timestamp.timestamp()) // interval_sec
                reset_anchor = (
                    anchor_balance is None
                    or events_applied
                    or bucket != previous_bucket
                )
                previous_bucket = bucket

                if reset_anchor or not active:
                    anchor_balance = float(row["balance"])
                    anchor_positions = dict(active)
                    prices = _prices_for_positions(
                        anchor_positions,
                        price_lookup,
                        timestamp,
                    )
                    anchor_prices = prices or {}
                    continue

                current_prices = _prices_for_positions(
                    active,
                    price_lookup,
                    timestamp,
                )
                if (
                    current_prices is None
                    or set(anchor_positions) != set(active)
                    or any(
                        position.symbol not in anchor_prices
                        for position in active.values()
                    )
                ):
                    missing_price_rows += 1
                    continue

                pnl_delta = sum(
                    position.pnl(current_prices[position.symbol])
                    - position.pnl(anchor_prices[position.symbol])
                    for position in active.values()
                )
                balance = anchor_balance + pnl_delta
                realized = float(row["realized_pnl"] or 0.0)
                original_unrealized = float(row["unrealized_pnl"] or 0.0)
                capital = float(row["balance"]) - realized - original_unrealized
                unrealized = balance - capital - realized

                if (
                    abs(balance - float(row["balance"])) <= 1e-9
                    and abs(unrealized - original_unrealized) <= 1e-9
                ):
                    continue

                snapshot_connection.execute(
                    """
                    UPDATE equity_snapshots
                    SET balance = ?, unrealized_pnl = ?
                    WHERE id = ?
                    """,
                    (balance, unrealized, int(row["id"])),
                )
                changed_rows += 1

        if dry_run:
            snapshot_connection.rollback()
        else:
            snapshot_connection.commit()
    except Exception:
        snapshot_connection.rollback()
        raise
    finally:
        snapshot_connection.close()

    return RepairResult(
        alpha_count=repaired_alphas,
        scanned_rows=scanned_rows,
        changed_rows=changed_rows,
        missing_price_rows=missing_price_rows,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair flat shadow equity history from virtual trades and MDS",
    )
    parser.add_argument("--trade-db", required=True)
    parser.add_argument("--snapshot-db", required=True)
    parser.add_argument("--mds-cache", required=True)
    parser.add_argument("--before", required=True, help="Exclusive UTC cutoff")
    parser.add_argument("--market-timeframe", default="15m")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    result = repair_shadow_equity(
        trade_db_path=args.trade_db,
        snapshot_db_path=args.snapshot_db,
        price_lookup=MdsPriceLookup(args.mds_cache, args.market_timeframe),
        before=args.before,
        dry_run=args.dry_run,
    )
    action = "would change" if args.dry_run else "changed"
    print(
        f"Shadow equity repair: alphas={result.alpha_count} "
        f"scanned={result.scanned_rows} {action}={result.changed_rows} "
        f"missing_price_rows={result.missing_price_rows}"
    )


if __name__ == "__main__":
    main()
