#!/usr/bin/env python3
"""Export paper-trade history to CSV.

Usage:
    python scripts/export_trades.py <alpha_id>
    python scripts/export_trades.py <alpha_id> --out my_trades.csv
    python scripts/export_trades.py <alpha_id> --db /path/to/paper-trade.db
    python scripts/export_trades.py              # export ALL alphas

Examples:
    python scripts/export_trades.py alpha-1-fixed
    python scripts/export_trades.py alpha-1-scale --out scale_trades.csv
    python scripts/export_trades.py wilder --db data/paper-trade.db
"""

import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "data", "paper-trade.db")

FIELDS = [
    "trade_id", "alpha_id", "symbol", "side",
    "entry_price", "exit_price", "qty", "leverage",
    "pnl", "pnl_percent", "fee",
    "tp", "sl", "reason",
    "duration_hours", "opened_at", "closed_at",
    "exchange", "metadata",
]


def list_alphas(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute("SELECT alpha_id FROM alphas ORDER BY created_at")
    return [r[0] for r in cur.fetchall()]


def export(conn: sqlite3.Connection, alpha_id: str | None, out_path: str) -> int:
    if alpha_id:
        cur = conn.execute(
            f"SELECT {', '.join(FIELDS)} FROM trades WHERE alpha_id = ? ORDER BY closed_at",
            (alpha_id,),
        )
    else:
        cur = conn.execute(
            f"SELECT {', '.join(FIELDS)} FROM trades ORDER BY alpha_id, closed_at"
        )

    rows = cur.fetchall()
    if not rows:
        return 0

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        writer.writerows(rows)

    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Export paper-trade history to CSV")
    parser.add_argument("alpha_id", nargs="?", default=None,
                        help="Alpha ID to export (omit to export all alphas)")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"Path to SQLite DB (default: {DEFAULT_DB})")
    parser.add_argument("--out", default=None,
                        help="Output CSV path (default: <alpha_id>_trades.csv or all_trades.csv)")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    alphas = list_alphas(conn)
    if not alphas:
        print("No alphas registered in DB yet.")
        sys.exit(0)

    print(f"Registered alphas: {', '.join(alphas)}")

    alpha_id = args.alpha_id
    if alpha_id and alpha_id not in alphas:
        print(f"WARNING: '{alpha_id}' not found in DB. Available: {', '.join(alphas)}")

    if args.out:
        out_path = args.out
    elif alpha_id:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = f"{alpha_id}_trades_{stamp}.csv"
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = f"all_trades_{stamp}.csv"

    n = export(conn, alpha_id, out_path)
    conn.close()

    if n == 0:
        print(f"No closed trades found for alpha_id='{alpha_id or 'ALL'}'")
    else:
        print(f"Exported {n} trades → {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
