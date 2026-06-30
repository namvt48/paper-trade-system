#!/usr/bin/env python3
"""Backfill equity_snapshots.db from historical trade + MDS parquet data.

Reconstructs account balance at each 15m boundary:
  balance = CAPITAL + realized_pnl + unrealized_pnl

Open positions at boundary T are reconstructed from trades.opened_at/closed_at.
Historical prices are read from MDS parquet files (base + delta).

Usage:
  python scripts/backfill_equity.py \
    --trade-db data/paper-trade.db \
    --snapshot-db data/equity-snapshots.db \
    --alphas-dir alphas \
    --mds-cache /root/market-data-service/historical_cache/binance \
    --interval-minutes 15
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TOTAL_KEY = "__TOTAL__"
DEFAULT_CAPITAL = 10000.0


def parse_env_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                trimmed = line.strip()
                if not trimmed or trimmed.startswith("#"):
                    continue
                eq = trimmed.find("=")
                if eq == -1:
                    continue
                key = trimmed[:eq].strip()
                val = trimmed[eq + 1:].split("#")[0].strip()
                result[key] = val
    except OSError:
        pass
    return result


def load_alpha_capitals(alphas_dir: str) -> dict[str, float]:
    capitals: dict[str, float] = {}
    try:
        entries = os.listdir(alphas_dir)
    except OSError:
        return capitals
    for entry in entries:
        env_path = os.path.join(alphas_dir, entry, ".env")
        if not os.path.isfile(env_path):
            continue
        parsed = parse_env_file(env_path)
        alpha_id = parsed.get("ALPHA_ID", entry)
        capital_str = parsed.get("CAPITAL", str(DEFAULT_CAPITAL))
        try:
            capitals[alpha_id] = float(capital_str)
        except ValueError:
            capitals[alpha_id] = DEFAULT_CAPITAL
    return capitals


def parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def floor_to_interval(dt: datetime, minutes: int) -> datetime:
    discard = dt.minute % minutes
    return dt.replace(minute=dt.minute - discard, second=0, microsecond=0)


class PriceLookup:
    """Reads MDS parquet (base + delta) per symbol, builds timestamp -> close price map."""

    def __init__(self, mds_cache_dir: str, timeframe: str = "15m"):
        self._dir = Path(mds_cache_dir) / timeframe
        self._cache: dict[str, dict[int, float]] = {}

    def _load_symbol(self, symbol: str) -> dict[int, float]:
        if symbol in self._cache:
            return self._cache[symbol]

        sym_dir = self._dir / symbol
        prices: dict[int, float] = {}

        if not sym_dir.is_dir():
            logger.warning("no MDS parquet for %s", symbol)
            self._cache[symbol] = prices
            return prices

        for parquet_file in sorted(sym_dir.iterdir()):
            if not parquet_file.name.endswith(".parquet"):
                continue
            try:
                table = pq.read_table(parquet_file, columns=["open_time", "close"])
                for ot, close in zip(table.column("open_time").to_pylist(),
                                     table.column("close").to_pylist()):
                    prices[ot] = float(close)
            except Exception:
                logger.exception("failed reading %s", parquet_file)

        self._cache[symbol] = prices
        return prices

    def get_price(self, symbol: str, ts_ms: int) -> float | None:
        prices = self._load_symbol(symbol)
        return prices.get(ts_ms)


def compute_position_pnl(side: str, entry_price: float, qty: float,
                         fee_pct: float, current_price: float) -> float:
    direction = 1.0 if side == "LONG" else -1.0
    gross = (current_price - entry_price) * qty * direction
    fee = (entry_price + current_price) * qty * fee_pct
    return gross - fee


def backfill(trade_db_path: str, snapshot_db_path: str, alphas_dir: str,
             mds_cache_dir: str, interval_minutes: int) -> int:
    capitals = load_alpha_capitals(alphas_dir)

    trade_conn = sqlite3.connect(trade_db_path)
    trade_conn.row_factory = sqlite3.Row

    alpha_created: dict[str, datetime] = {}
    for row in trade_conn.execute("SELECT alpha_id, created_at FROM alphas").fetchall():
        try:
            dt = parse_ts(row["created_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            alpha_created[row["alpha_id"]] = dt
        except Exception:
            pass

    first_trade_ts: dict[str, datetime] = {}
    for row in trade_conn.execute(
        "SELECT alpha_id, MIN(opened_at) as first FROM trades GROUP BY alpha_id"
    ).fetchall():
        if row["first"]:
            try:
                dt = parse_ts(row["first"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                first_trade_ts[row["alpha_id"]] = dt
            except Exception:
                pass

    trades = trade_conn.execute(
        "SELECT alpha_id, symbol, side, entry_price, qty, fee, pnl, opened_at, closed_at "
        "FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at ASC"
    ).fetchall()

    positions = trade_conn.execute(
        "SELECT alpha_id, symbol, side, entry_price, qty, fee_pct, opened_at "
        "FROM positions ORDER BY opened_at ASC"
    ).fetchall()

    trade_conn.close()

    if not trades and not positions:
        logger.info("no trades or positions found, nothing to backfill")
        return 0

    all_ts_raw = [r["opened_at"] for r in trades if r["opened_at"]] + \
                 [r["closed_at"] for r in trades if r["closed_at"]] + \
                 [r["opened_at"] for r in positions if r["opened_at"]]
    first_ts = min(parse_ts(t) for t in all_ts_raw)
    now = datetime.now(timezone.utc)
    end_ts = now

    start_floor = floor_to_interval(first_ts, interval_minutes)
    boundaries: list[datetime] = []
    cur = start_floor
    while cur <= end_ts:
        boundaries.append(cur)
        cur += timedelta(minutes=interval_minutes)

    logger.info("%d trades, %d positions, %d %dm boundaries from %s to %s",
                len(trades), len(positions), len(boundaries), interval_minutes,
                start_floor.isoformat(), end_ts.isoformat())

    all_symbols = {r["symbol"] for r in trades} | {r["symbol"] for r in positions}
    price_lookup = PriceLookup(mds_cache_dir, f"{interval_minutes}m")

    snap_conn = sqlite3.connect(snapshot_db_path)
    snap_conn.execute("PRAGMA journal_mode=WAL")
    snap_conn.execute("PRAGMA synchronous=NORMAL")
    snap_conn.execute("PRAGMA busy_timeout=5000")
    snap_conn.executescript("""
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            alpha_id TEXT NOT NULL,
            balance REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_equity_snapshots_alpha_time
            ON equity_snapshots(alpha_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_equity_snapshots_time
            ON equity_snapshots(timestamp);
    """)

    min_forward_row = snap_conn.execute(
        "SELECT MIN(timestamp) FROM equity_snapshots"
    ).fetchone()
    min_forward_ts = min_forward_row[0]

    if min_forward_ts:
        snap_conn.execute("DELETE FROM equity_snapshots WHERE timestamp < ?", (min_forward_ts,))
        end_ts = parse_ts(min_forward_ts)
        boundaries = [b for b in boundaries if b < end_ts]
        logger.info("preserving forward data from %s, backfilling %d boundaries", min_forward_ts, len(boundaries))
    else:
        snap_conn.execute("DELETE FROM equity_snapshots")

    snap_conn.commit()

    alpha_ids = set(capitals.keys())
    alpha_ids.update(r["alpha_id"] for r in trades)
    alpha_ids.update(r["alpha_id"] for r in positions)

    trade_idx = 0
    cumulative_realized: dict[str, float] = defaultdict(float)
    total_realized = 0.0
    batch: list[tuple[str, str, float]] = []
    inserted = 0
    missing_price_count = 0

    for boundary in boundaries:
        b_ms = int(boundary.timestamp() * 1000)
        b_iso = boundary.isoformat()

        while trade_idx < len(trades):
            t = trades[trade_idx]
            t_closed = t["closed_at"]
            if not t_closed:
                trade_idx += 1
                continue
            if parse_ts(t_closed) > boundary:
                break
            pnl = t["pnl"] or 0.0
            cumulative_realized[t["alpha_id"]] += pnl
            total_realized += pnl
            trade_idx += 1

        open_positions = []
        for t in trades:
            t_opened = t["opened_at"]
            t_closed = t["closed_at"]
            if not t_opened:
                continue
            if parse_ts(t_opened) > boundary:
                continue
            if t_closed and parse_ts(t_closed) <= boundary:
                continue
            open_positions.append(t)

        for p in positions:
            p_opened = p["opened_at"]
            if not p_opened:
                continue
            if parse_ts(p_opened) <= boundary:
                open_positions.append(p)

        unrealized_by_alpha: dict[str, float] = defaultdict(float)
        for pos in open_positions:
            symbol = pos["symbol"]
            price = price_lookup.get_price(symbol, b_ms)
            if price is None:
                missing_price_count += 1
                continue
            if "fee_pct" in pos.keys():
                fee_pct = pos["fee_pct"] or 0.0
            elif "fee" in pos.keys() and "exit_price" in pos.keys():
                total_value = (pos["entry_price"] + pos["exit_price"]) * pos["qty"]
                fee_pct = (pos["fee"] / total_value) if total_value > 0 else 0.0
            else:
                fee_pct = 0.0
            pnl = compute_position_pnl(
                pos["side"], pos["entry_price"], pos["qty"],
                fee_pct, price
            )
            unrealized_by_alpha[pos["alpha_id"]] += pnl

        total_unrealized = sum(unrealized_by_alpha.values())

        active_alphas = set()
        for aid in alpha_ids:
            created = alpha_created.get(aid)
            first_trade = first_trade_ts.get(aid)
            earliest = None
            if created and first_trade:
                earliest = min(created, first_trade)
            elif created:
                earliest = created
            elif first_trade:
                earliest = first_trade
            if earliest is None or earliest <= boundary:
                active_alphas.add(aid)

        total_capital = sum(capitals.get(a, DEFAULT_CAPITAL) for a in active_alphas)
        total_balance = total_capital + total_realized + total_unrealized

        for aid in sorted(active_alphas):
            cap = capitals.get(aid, DEFAULT_CAPITAL)
            real = cumulative_realized.get(aid, 0.0)
            unreal = unrealized_by_alpha.get(aid, 0.0)
            balance = cap + real + unreal
            batch.append((b_iso, aid, balance))

        batch.append((b_iso, TOTAL_KEY, total_balance))
        inserted += len(active_alphas) + 1

        if len(batch) > 10000:
            snap_conn.executemany(
                "INSERT INTO equity_snapshots (timestamp, alpha_id, balance) VALUES (?, ?, ?)",
                batch
            )
            snap_conn.commit()
            batch.clear()

    if batch:
        snap_conn.executemany(
            "INSERT INTO equity_snapshots (timestamp, alpha_id, balance) VALUES (?, ?, ?)",
            batch
        )
        snap_conn.commit()

    snap_conn.close()

    if missing_price_count:
        logger.warning("%d positions skipped due to missing price data", missing_price_count)
    logger.info("inserted %d rows across %d boundaries", inserted, len(boundaries))
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Backfill equity snapshots from trade history + MDS parquet")
    parser.add_argument("--trade-db", required=True, help="Path to paper-trade.db")
    parser.add_argument("--snapshot-db", required=True, help="Path to equity-snapshots.db")
    parser.add_argument("--alphas-dir", required=True, help="Path to alphas/ directory")
    parser.add_argument("--mds-cache", required=True, help="Path to MDS historical_cache/binance")
    parser.add_argument("--interval-minutes", type=int, default=15)
    args = parser.parse_args()

    count = backfill(
        args.trade_db, args.snapshot_db, args.alphas_dir,
        args.mds_cache, args.interval_minutes
    )
    print(f"Backfill complete: {count} rows inserted into {args.snapshot_db}")


if __name__ == "__main__":
    main()
