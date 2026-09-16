#!/usr/bin/env python3
"""Close orphaned positions left behind by inactive alphas.

When an alpha disappears from runner-config.yaml it stops being managed: the
runner cancels its manage_loop (alphas/runner/main.py `_on_disabled`) and the
worker's TPSL auto-close is off by default, so a position it still holds has no
exit path left and freezes open forever.

This republishes one CLOSE per orphaned position through the normal
`paper-signals` stream, so the worker writes a real trade (slippage, fees, PnL)
and clears the position row -- no direct DB writes, no duplicated close logic.

Exit price is the durable `last_mark_prices` value the equity collector already
marks the position with, so realised PnL matches the last displayed unrealised
PnL instead of collapsing to zero. `exit_price` must be set explicitly: without
it the worker falls back to `entry_price` whenever the MDS fill RPC is down,
which would record ~0 PnL on every trade.

Dry run by default.

    python close_orphaned_positions.py            # report only
    python close_orphaned_positions.py --execute  # publish CLOSE signals
"""

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import redis

PAPER_DB = os.environ.get("PAPER_DB", "/app/data/paper-trade.db")
SNAPSHOT_DB = os.environ.get("SNAPSHOT_DB", "/app/data/equity-snapshots.db")
REDIS_URL = os.environ.get("REDIS_URL", "redis://paper-redis:6379")
STREAM = os.environ.get("REDIS_STREAM", "paper-signals")
REASON = "ALPHA_DISABLED_ORPHAN"


def load_positions(paper_db: str) -> list[dict[str, Any]]:
    """Open positions belonging to alphas marked inactive."""
    con = sqlite3.connect(f"file:{paper_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT p.position_id, p.alpha_id, p.symbol, p.side, p.qty,
                   p.entry_price, p.exchange, p.status
              FROM positions p
              JOIN alphas a ON a.alpha_id = p.alpha_id
             WHERE a.status = 'inactive'
             ORDER BY p.alpha_id, p.symbol
            """
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def load_marks(snapshot_db: str) -> dict[str, float]:
    con = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    try:
        return {
            str(sym): float(price)
            for sym, price in con.execute("SELECT symbol, price FROM last_mark_prices")
        }
    finally:
        con.close()


def build_signal(pos: dict[str, Any], mark: float, now: str) -> dict[str, Any]:
    # Deterministic signal_id: re-running cannot double-close, because the
    # second attempt targets a position_id the first run already removed.
    return {
        "type": "CLOSE",
        "alpha_id": pos["alpha_id"],
        "signal_id": f"orphan-close-{pos['position_id']}",
        "position_id": pos["position_id"],
        "symbol": pos["symbol"],
        "reason": REASON,
        "timestamp": now,
        "exit_price": repr(float(mark)),
        "metadata": json.dumps(
            {"orphan_cleanup": True, "mark_price": mark, "uuid": str(uuid.uuid4())}
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="publish (default: dry run)")
    ap.add_argument("--batch", type=int, default=200, help="signals per pipeline")
    args = ap.parse_args()

    positions = load_positions(PAPER_DB)
    if not positions:
        print("no orphaned positions -- nothing to do")
        return 0

    marks = load_marks(SNAPSHOT_DB)

    no_mark = sorted(
        {p["symbol"] for p in positions if not marks.get(p["symbol"], 0) > 0}
    )
    if no_mark:
        print(f"ABORT: {len(no_mark)} symbol(s) missing a mark price: {no_mark[:10]}")
        return 1

    unmarkable = [
        p["position_id"] for p in positions if not p["qty"] or not p["entry_price"]
    ]
    if unmarkable:
        print(
            f"ABORT: {len(unmarkable)} position(s) with NULL/0 qty or entry: {unmarkable[:10]}"
        )
        return 1

    now = datetime.now(timezone.utc).isoformat()
    signals = [build_signal(p, marks[p["symbol"]], now) for p in positions]
    alphas = sorted({p["alpha_id"] for p in positions})
    statuses = sorted({p["status"] for p in positions})

    print(f"positions={len(positions)} alphas={len(alphas)} signals={len(signals)}")
    print(f"position.status values seen: {statuses}")
    print(f"distinct symbols={len({p['symbol'] for p in positions})}")

    if not args.execute:
        print("\nDRY RUN -- nothing published. First 2 signals:")
        for s in signals[:2]:
            print(json.dumps(s, indent=2))
        return 0

    r: Any = redis.from_url(REDIS_URL, decode_responses=True)
    published = 0
    for i in range(0, len(signals), args.batch):
        chunk = signals[i : i + args.batch]
        pipe = r.pipeline(transaction=False)
        for s in chunk:
            pipe.xadd(STREAM, s)
        pipe.execute()
        published += len(chunk)
        print(f"published {published}/{len(signals)}")
    print("done -- worker drains the stream asynchronously; verify positions==0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
