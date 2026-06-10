#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3

import redis


def main() -> int:
    db_path = os.getenv("DB_PATH", "data/paper-trade.db")
    paper = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6382"), decode_responses=True)
    mds = redis.from_url(os.getenv("MDS_REDIS_URL", "redis://localhost:6381"), decode_responses=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    positions = [dict(row) for row in conn.execute("SELECT * FROM positions ORDER BY alpha_id, symbol")]
    failures = []
    for pos in positions:
        alpha = pos["alpha_id"]
        heartbeat_raw = paper.get(f"paper:alpha-runtime:{alpha}")
        heartbeat = json.loads(heartbeat_raw) if heartbeat_raw else {}
        managed = set(heartbeat.get("managed_position_ids", []))
        desired = set(heartbeat.get("desired_price_alert_symbols", []))
        exchange = str(pos.get("exchange") or "binance").lower()
        actual = set(mds.smembers(f"price_alert:subscriptions:{exchange}:{alpha}"))
        issues = []
        if pos["position_id"] not in managed:
            issues.append("missing_owner")
        if pos["symbol"] not in desired:
            issues.append("missing_desired_subscription")
        if pos["symbol"] not in actual:
            issues.append("missing_mds_subscription")
        if issues:
            failures.append({**pos, "issues": issues})
    print(json.dumps({
        "position_count": len(positions),
        "healthy": not failures,
        "failures": failures,
    }, indent=2, default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
