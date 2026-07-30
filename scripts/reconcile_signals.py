#!/usr/bin/env python3
"""Reconcile the signal pipeline end-to-end: producer -> Redis stream -> worker DB.

Proves the "no silent failures" invariant for signals. Every signal that was
published must be accounted for as either committed, errored, in-flight
(pending), or undelivered (lag). Anything left over is a SILENT DROP.

    published (XLEN)  ==  committed + errored + pending + lag + gap
    gap > tolerance   ->  FAIL (exit 1)

Benign reductions (dedup at producer, duplicate-skip at consumer) do NOT show
up as gap: dedup happens before XADD, duplicate-skip happens before the DB row
is written, so neither inflates XLEN nor the DB counts.

Usage:
    python scripts/reconcile_signals.py \
        --db data/paper-trade.db \
        --redis-url redis://localhost:6379 \
        --stream paper-signals \
        --group paper-executor \
        [--since 2026-07-17T00:00:00] \
        [--tolerance 0]

Read-only. Safe to run against a live DB (opens SQLite read-only) and a live
stream (only XLEN/XINFO/XPENDING, no XADD/XACK/XREADGROUP).
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reconcile")


# --------------------------------------------------------------------------- #
# Pure collection + reconciliation (no I/O side effects here → unit testable)  #
# --------------------------------------------------------------------------- #

def collect_db_stats(conn: sqlite3.Connection, since: str | None = None) -> dict[str, int]:
    """Count worker-side signal outcomes from SQLite. `since` filters signals by
    received_at (ISO text, lexicographically comparable)."""
    where = ""
    params: tuple[Any, ...] = ()
    if since:
        where = " WHERE received_at >= ?"
        params = (since,)

    def one(sql: str, p: tuple[Any, ...] = ()) -> int:
        row = conn.execute(sql, p).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    return {
        "signals_rows": one(f"SELECT COUNT(*) FROM signals{where}", params),
        "signals_distinct": one(
            f"SELECT COUNT(DISTINCT signal_id) FROM signals{where}", params),
        "signals_committed": one(
            f"SELECT COUNT(*) FROM signals{where}{' AND' if where else ' WHERE'} "
            "processed = 1 AND error IS NULL", params),
        "signals_errored": one(
            f"SELECT COUNT(*) FROM signals{where}{' AND' if where else ' WHERE'} "
            "error IS NOT NULL", params),
        "trades": one("SELECT COUNT(*) FROM trades"),
        "positions": one("SELECT COUNT(*) FROM positions"),
    }


def collect_stream_stats(redis_client, stream: str, group: str) -> dict[str, int]:
    """Read transport-level counts from the Redis stream + consumer group."""
    try:
        xlen = int(redis_client.xlen(stream))
    except Exception:
        xlen = 0

    pending = 0
    lag = 0
    entries_read = 0
    try:
        for g in redis_client.xinfo_groups(stream):
            name = g.get("name") if isinstance(g, dict) else None
            if isinstance(name, bytes):
                name = name.decode()
            if name != group:
                continue
            pending = int(g.get("pending", 0) or 0)
            lag = int(g.get("lag", 0) or 0)
            entries_read = int(g.get("entries-read", 0) or 0)
    except Exception:
        pass

    return {"xlen": xlen, "pending": pending, "lag": lag, "entries_read": entries_read}


@dataclass
class ReconcileResult:
    published: int
    committed: int
    errored: int
    pending: int
    lag: int
    gap: int
    tolerance: int
    ok: bool
    db_stats: dict[str, int] = field(default_factory=dict)
    stream_stats: dict[str, int] = field(default_factory=dict)
    producer_stats: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "published_xlen": self.published,
            "committed": self.committed,
            "errored": self.errored,
            "pending_in_flight": self.pending,
            "lag_undelivered": self.lag,
            "gap_unexplained": self.gap,
            "tolerance": self.tolerance,
            "ok": self.ok,
            "db_stats": self.db_stats,
            "stream_stats": self.stream_stats,
            "producer_stats": self.producer_stats,
        }


def reconcile(db_stats: dict[str, int], stream_stats: dict[str, int],
              producer_stats: dict[str, Any] | None = None,
              tolerance: int = 0) -> ReconcileResult:
    """Compute the invariant. gap = published - (committed + errored + pending + lag).

    A positive gap means signals were published but are neither recorded in the
    DB nor in-flight → silent drop. A negative gap (DB has more than the stream)
    can happen when the stream was trimmed after the rows were written; it is
    also flagged (|gap| > tolerance) so the operator investigates.
    """
    published = int(stream_stats.get("xlen", 0))
    committed = int(db_stats.get("signals_committed", 0))
    errored = int(db_stats.get("signals_errored", 0))
    pending = int(stream_stats.get("pending", 0))
    lag = int(stream_stats.get("lag", 0))

    gap = published - (committed + errored + pending + lag)
    ok = abs(gap) <= tolerance
    return ReconcileResult(
        published=published, committed=committed, errored=errored,
        pending=pending, lag=lag, gap=gap, tolerance=tolerance, ok=ok,
        db_stats=dict(db_stats), stream_stats=dict(stream_stats),
        producer_stats=producer_stats,
    )


def format_report(result: ReconcileResult) -> str:
    r = result
    lines = [
        "=== signal reconciliation ===",
        f"  published (XLEN)      : {r.published}",
        f"  committed             : {r.committed}",
        f"  errored               : {r.errored}",
        f"  pending (in-flight)   : {r.pending}",
        f"  lag (undelivered)     : {r.lag}",
        f"  ---------------------",
        f"  gap (unexplained)     : {r.gap}  (tolerance={r.tolerance})",
        f"  RESULT                : {'OK' if r.ok else 'FAIL — SILENT DROP'}",
        f"  db context            : trades={r.db_stats.get('trades')} "
        f"positions={r.db_stats.get('positions')} "
        f"signals_rows={r.db_stats.get('signals_rows')} "
        f"signals_distinct={r.db_stats.get('signals_distinct')}",
    ]
    if r.producer_stats:
        lines.append(f"  producer              : {json.dumps(r.producer_stats)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI wiring                                                                    #
# --------------------------------------------------------------------------- #

def _open_readonly(db_path: str) -> sqlite3.Connection:
    # Read-only URI so a live worker keeps writing undisturbed.
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def run(db_path: str, redis_url: str, stream: str, group: str,
        since: str | None = None, tolerance: int = 0,
        producer_metrics_url: str | None = None) -> ReconcileResult:
    conn = _open_readonly(db_path)
    try:
        db_stats = collect_db_stats(conn, since=since)
    finally:
        conn.close()

    import redis  # local import so unit tests of pure fns need no redis
    redis_client = redis.from_url(redis_url, decode_responses=True)
    stream_stats = collect_stream_stats(redis_client, stream, group)

    producer_stats = None
    if producer_metrics_url:
        try:
            import urllib.request
            with urllib.request.urlopen(producer_metrics_url, timeout=5) as resp:
                snap = json.loads(resp.read().decode())
            producer_stats = {
                k: snap.get(k) for k in (
                    "signals_dispatched_total", "signals_dedup_skipped_total",
                    "signals_lease_dropped_total", "signals_xadd_published_total",
                    "signals_lease_dropped_by_alpha",
                )
            }
        except Exception as exc:  # producer metrics are advisory, never fatal
            logger.warning("could not read producer metrics: %s", exc)

    return reconcile(db_stats, stream_stats, producer_stats=producer_stats,
                     tolerance=tolerance)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile the signal pipeline")
    parser.add_argument("--db", default="data/paper-trade.db")
    parser.add_argument("--redis-url", default="redis://localhost:6379")
    parser.add_argument("--stream", default="paper-signals")
    parser.add_argument("--group", default="paper-executor")
    parser.add_argument("--since", default=None,
                        help="ISO timestamp; filter signals by received_at (default: all)")
    parser.add_argument("--tolerance", type=int, default=0)
    parser.add_argument("--producer-metrics-url", default=None,
                        help="optional runner /metrics URL for producer-side cross-check")
    args = parser.parse_args(argv)

    result = run(args.db, args.redis_url, args.stream, args.group,
                 since=args.since, tolerance=args.tolerance,
                 producer_metrics_url=args.producer_metrics_url)

    print(format_report(result))
    if not result.ok:
        logger.error("[RECONCILE] unexplained gap=%d exceeds tolerance=%d",
                     result.gap, result.tolerance)
        return 1
    logger.info("[RECONCILE] ok, gap=%d within tolerance=%d", result.gap, result.tolerance)
    return 0


if __name__ == "__main__":
    sys.exit(main())
