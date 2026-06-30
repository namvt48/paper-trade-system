#!/usr/bin/env python3
"""Ensure every alpha's open-position total is even in SQLite.

For each alpha:
  - If the total number of open positions is odd → delete exactly one
    position from the larger side, choosing the one with the lowest live PnL.
  - If already even → no action.

Live PnL is computed from Binance spot prices.  If a symbol's price cannot be
fetched, its PnL defaults to 0 (neutral) so it is deleted before profitable
positions.

Usage:
    python3 scripts/balance_positions.py            # dry-run, print plan
    python3 scripts/balance_positions.py --execute  # apply deletions
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "paper-trade.db"
BINANCE_PRICES_URL = "https://api.binance.com/api/v3/ticker/price"


def fetch_binance_prices() -> dict[str, float]:
    """Return {symbol: price} for all Binance spot symbols."""
    try:
        with urllib.request.urlopen(BINANCE_PRICES_URL, timeout=15) as resp:
            raw = json.loads(resp.read())
        return {item["symbol"]: float(item["price"]) for item in raw}
    except Exception as exc:
        print(f"[WARN] Failed to fetch Binance prices: {exc}", file=sys.stderr)
        return {}


def compute_pnl(position: dict, prices: dict[str, float]) -> float:
    """Live PnL for an open position.  Returns 0 if price unavailable."""
    symbol = position["symbol"]
    entry = position["entry_price"]
    qty = position["qty"]
    side = position["side"]
    fee_pct = position.get("fee_pct") or 0.0

    current = prices.get(symbol)
    if current is None or current <= 0:
        return 0.0

    direction = 1.0 if side == "LONG" else -1.0
    gross = (current - entry) * qty * direction
    fee = (entry + current) * qty * fee_pct
    return gross - fee


def load_positions(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Return {alpha_id: [position_rows]} for all open positions."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT position_id, alpha_id, symbol, side, entry_price, qty, fee_pct "
        "FROM positions ORDER BY alpha_id, side, opened_at"
    ).fetchall()
    by_alpha: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_alpha[row["alpha_id"]].append(dict(row))
    return by_alpha


def plan_deletions(positions: list[dict], prices: dict[str, float]) -> list[str]:
    """Return position_ids to delete for one alpha to balance LONG = SHORT."""
    longs = [p for p in positions if p["side"] == "LONG"]
    shorts = [p for p in positions if p["side"] == "SHORT"]

    to_delete: list[dict] = []

    if len(positions) % 2 != 0:
        larger = longs if len(longs) >= len(shorts) else shorts
        if larger:
            ranked = sorted(larger, key=lambda p: compute_pnl(p, prices))
            to_delete.append(ranked[0])

    return [p["position_id"] for p in to_delete]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Apply deletions. Without this flag, runs as dry-run.")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to SQLite DB")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"[ERROR] DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    print("Fetching live prices from Binance...", file=sys.stderr)
    prices = fetch_binance_prices()
    print(f"  {len(prices)} symbols", file=sys.stderr)

    conn = sqlite3.connect(args.db)
    by_alpha = load_positions(conn)

    total_before = sum(len(v) for v in by_alpha.values())
    total_delete = 0

    print(f"\n{'Alpha':<55} {'Before':>8} {'L':>5} {'S':>5} {'Delete':>7} {'After':>7}")
    print("-" * 92)

    all_to_delete: list[str] = []

    for alpha_id in sorted(by_alpha):
        positions = by_alpha[alpha_id]
        longs = sum(1 for p in positions if p["side"] == "LONG")
        shorts = sum(1 for p in positions if p["side"] == "SHORT")
        before = len(positions)

        to_delete = plan_deletions(positions, prices)
        after = before - len(to_delete)
        total_delete += len(to_delete)
        all_to_delete.extend(to_delete)

        flag = "" if (longs == 0 or shorts == 0 or longs == shorts) else " *"
        print(f"{alpha_id:<55} {before:>8} {longs:>5} {shorts:>5} {len(to_delete):>7} {after:>7}{flag}")

    print("-" * 92)
    print(f"{'TOTAL':<55} {total_before:>8} {'':>5} {'':>5} {total_delete:>7} {total_before - total_delete:>7}")

    if total_delete == 0:
        print("\nAll alphas already balanced. Nothing to do.")
        conn.close()
        return

    if not args.execute:
        print(f"\n[DRY-RUN] {total_delete} positions would be deleted. Run with --execute to apply.")
        conn.close()
        return

    print(f"\n[EXECUTE] Deleting {total_delete} positions...", file=sys.stderr)
    conn.executemany("DELETE FROM positions WHERE position_id = ?",
                     [(pid,) for pid in all_to_delete])
    conn.commit()
    print(f"[EXECUTE] Done. {total_delete} positions deleted.", file=sys.stderr)

    # Verify
    remaining = load_positions(conn)
    print(f"\n{'Alpha':<55} {'After':>8} {'L':>5} {'S':>5} {'Even':>6}")
    print("-" * 81)
    all_even = True
    for alpha_id in sorted(remaining):
        positions = remaining[alpha_id]
        longs = sum(1 for p in positions if p["side"] == "LONG")
        shorts = sum(1 for p in positions if p["side"] == "SHORT")
        even = len(positions) % 2 == 0
        if not even:
            all_even = False
        print(f"{alpha_id:<55} {len(positions):>8} {longs:>5} {shorts:>5} {'OK' if even else 'ODD':>6}")

    print(f"\n{'ALL EVEN' if all_even else 'SOME ODD'}")
    conn.close()


if __name__ == "__main__":
    main()
