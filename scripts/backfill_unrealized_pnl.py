#!/usr/bin/env python3
"""Backfill unrealized_pnl and realized_pnl columns in equity_snapshots.db.

For each existing snapshot row, derives:
  realized_pnl   = SUM(pnl) FROM trades WHERE alpha_id = ? AND closed_at <= timestamp
  unrealized_pnl = balance - capital - realized_pnl

Requires the columns to already exist (worker migration adds them via
ALTER TABLE ADD COLUMN).

Usage:
  python scripts/backfill_unrealized_pnl.py \
    --trade-db data/paper-trade.db \
    --snapshot-db data/equity-snapshots.db \
    --alphas-dir alphas
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOTAL_KEY = "__TOTAL__"


def _parse_env_file(path: str) -> dict[str, str]:
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
        parsed = _parse_env_file(env_path)
        alpha_id = parsed.get("ALPHA_ID", entry)
        capital_str = parsed.get("CAPITAL", "10000.0")
        try:
            capitals[alpha_id] = float(capital_str)
        except ValueError:
            capitals[alpha_id] = 10000.0
    return capitals


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def backfill(trade_db_path: str, snapshot_db_path: str, alphas_dir: str) -> int:
    capitals = load_alpha_capitals(alphas_dir)

    trade_conn = sqlite3.connect(trade_db_path)
    trade_conn.row_factory = sqlite3.Row

    snap_conn = sqlite3.connect(snapshot_db_path)
    snap_conn.row_factory = sqlite3.Row
    snap_conn.execute("PRAGMA journal_mode=WAL")
    snap_conn.execute("PRAGMA busy_timeout=5000")

    cols = [r[1] for r in snap_conn.execute("PRAGMA table_info(equity_snapshots)").fetchall()]
    if "unrealized_pnl" not in cols or "realized_pnl" not in cols:
        logger.error("Columns unrealized_pnl/realized_pnl not found. Run worker migration first.")
        return 0

    rows = snap_conn.execute(
        "SELECT id, timestamp, alpha_id, balance FROM equity_snapshots ORDER BY timestamp ASC"
    ).fetchall()

    if not rows:
        logger.info("No snapshots to backfill")
        return 0

    trade_rows = trade_conn.execute(
        "SELECT alpha_id, pnl, closed_at FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at ASC"
    ).fetchall()

    trades_by_alpha: dict[str, list[tuple[str, float]]] = {}
    for tr in trade_rows:
        aid = tr["alpha_id"]
        trades_by_alpha.setdefault(aid, []).append(
            (tr["closed_at"], tr["pnl"] or 0.0)
        )

    trade_conn.close()

    updated = 0
    batch: list[tuple[float, float, int]] = []

    for row in rows:
        alpha_id = row["alpha_id"]
        ts = row["timestamp"]
        balance = row["balance"]

        if alpha_id == TOTAL_KEY:
            cap = 0.0
        else:
            cap = capitals.get(alpha_id, 10000.0)

        realized = 0.0
        alpha_trades = trades_by_alpha.get(alpha_id, [])
        try:
            ts_dt = _parse_ts(ts)
        except (ValueError, TypeError):
            ts_dt = datetime.min

        for trade_ts, trade_pnl in alpha_trades:
            try:
                trade_dt = _parse_ts(trade_ts)
            except (ValueError, TypeError):
                continue
            if trade_dt <= ts_dt:
                realized += trade_pnl

        unrealized = balance - cap - realized

        batch.append((unrealized, realized, row["id"]))
        updated += 1

        if len(batch) >= 5000:
            snap_conn.executemany(
                "UPDATE equity_snapshots SET unrealized_pnl = ?, realized_pnl = ? WHERE id = ?",
                batch,
            )
            snap_conn.commit()
            batch.clear()

    if batch:
        snap_conn.executemany(
            "UPDATE equity_snapshots SET unrealized_pnl = ?, realized_pnl = ? WHERE id = ?",
            batch,
        )
        snap_conn.commit()

    snap_conn.close()
    logger.info("Updated %d snapshot rows", updated)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill unrealized_pnl and realized_pnl in equity_snapshots"
    )
    parser.add_argument("--trade-db", required=True, help="Path to paper-trade.db")
    parser.add_argument("--snapshot-db", required=True, help="Path to equity-snapshots.db")
    parser.add_argument("--alphas-dir", required=True, help="Path to alphas/ directory")
    args = parser.parse_args()

    count = backfill(args.trade_db, args.snapshot_db, args.alphas_dir)
    print(f"Backfill complete: {count} rows updated in {args.snapshot_db}")


if __name__ == "__main__":
    main()
