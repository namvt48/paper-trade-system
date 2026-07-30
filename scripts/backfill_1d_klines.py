#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "redis>=5.0",
# ]
# ///
"""Backfill confirmed 1d klines from Binance REST into the MDS Redis snapshot.

Restores the `kline_snapshot_v2:{exchange}:1d:{symbol}` lists that the runner
warmup and the staged rebalance recovery read. Needed after the MDS 1d rollup
stopped producing daily candles (HANDOFF cập nhật 15).

Read-only by default (`--dry-run`). A real write first dumps every touched key to
a rollback file, then rewrites each list newest-first with authoritative Binance
candles merged over whatever already exists (Binance wins on same open_time).

Usage (server, off-peak):
  uv run scripts/backfill_1d_klines.py \
    --redis-url redis://localhost:6381 \
    --whitelist alphas/1d-kertrend/whitelist.txt --whitelist alphas/1d-chmom/whitelist.txt ... \
    --dry-run
  # then, after review, drop --dry-run and add --apply
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import redis

FAPI = "https://fapi.binance.com/fapi/v1/klines"
SNAPSHOT_LIMIT = 300  # SNAPSHOT_MAX_CANDLES_1D in MDS config


def load_universe(whitelist_paths: list[Path]) -> list[str]:
    symbols: set[str] = set()
    for path in whitelist_paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.add(sym)
    return sorted(symbols)


def fetch_klines(symbol: str, interval: str, limit: int, now_ms: int) -> list[dict]:
    """Return confirmed (closed) candle dicts, oldest→newest."""
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    req = urllib.request.Request(f"{FAPI}?{qs}", headers={"User-Agent": "mds-backfill/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode())
    candles: list[dict] = []
    for k in raw:
        close_time = int(k[6])
        if close_time >= now_ms:
            continue  # skip the still-forming current candle
        candles.append(
            {
                "symbol": symbol,
                "tf": interval,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "open_time": int(k[0]),
                "close_time": close_time,
                "confirmed": True,
                "exchange": "binance",
            }
        )
    return candles


def merge_existing(client: redis.Redis, key: str, fetched: list[dict]) -> list[dict]:
    """Merge Binance candles over whatever exists; Binance wins on open_time."""
    by_time: dict[int, dict] = {}
    for raw in client.lrange(key, 0, -1):
        try:
            row = json.loads(raw)
            ot = int(row.get("open_time", row.get("time", 0)))
            if ot > 0:
                by_time[ot] = row
        except (ValueError, TypeError):
            continue
    for row in fetched:
        by_time[int(row["open_time"])] = row  # authoritative overwrite
    return [by_time[t] for t in sorted(by_time, reverse=True)]  # newest-first


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis-url", required=True)
    ap.add_argument("--whitelist", action="append", type=Path, required=True)
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=95)
    ap.add_argument("--now-ms", type=int, required=True, help="UTC now in ms (pass from `date`)")
    ap.add_argument("--sleep-ms", type=int, default=120)
    ap.add_argument("--backup", type=Path, default=Path("recovery/backfill-1d-redis-backup.json"))
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", dest="dry_run", action="store_false")
    ap.add_argument("--sample", type=int, default=5, help="dry-run: symbols to fetch")
    args = ap.parse_args()

    universe = load_universe(args.whitelist)
    print(f"[UNIVERSE] {len(universe)} symbols from {len(args.whitelist)} whitelist(s)")
    if not universe:
        print("[ERROR] empty universe", file=sys.stderr)
        return 2

    client = redis.from_url(args.redis_url, decode_responses=True, socket_timeout=10)

    if args.dry_run:
        print(f"[DRY-RUN] fetching first {args.sample} symbols, NO writes\n")
        for sym in universe[: args.sample]:
            key = f"kline_snapshot_v2:{args.exchange}:{args.interval}:{sym}"
            try:
                fetched = fetch_klines(sym, args.interval, args.limit, args.now_ms)
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"  {sym:14} FETCH-FAIL {exc}")
                continue
            existing_n = client.llen(key)
            merged = merge_existing(client, key, fetched)
            oldest = min(c["open_time"] for c in fetched)
            newest = max(c["open_time"] for c in fetched)

            def d(ms: int) -> str:
                return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))

            print(
                f"  {sym:14} fetched={len(fetched):3} range={d(oldest)}..{d(newest)} "
                f"existing_redis={existing_n} -> merged={len(merged)}"
            )
            if sym == "BTCUSDT":
                print(f"    newest candle: {json.dumps(fetched[-1])}")
            time.sleep(args.sleep_ms / 1000)
        print("\n[DRY-RUN] done. Re-run with --apply to write all symbols.")
        return 0

    # --- APPLY ---
    args.backup.parent.mkdir(parents=True, exist_ok=True)
    backup: dict[str, list[str]] = {}
    written = 0
    failed: list[str] = []
    for i, sym in enumerate(universe, 1):
        key = f"kline_snapshot_v2:{args.exchange}:{args.interval}:{sym}"
        try:
            fetched = fetch_klines(sym, args.interval, args.limit, args.now_ms)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(universe)}] {sym} FETCH-FAIL {exc}")
            failed.append(sym)
            continue
        if not fetched:
            failed.append(sym)
            continue
        backup[key] = client.lrange(key, 0, -1)  # snapshot for rollback
        merged = merge_existing(client, key, fetched)[:SNAPSHOT_LIMIT]
        pipe = client.pipeline(transaction=True)
        pipe.delete(key)
        pipe.rpush(key, *[json.dumps(c) for c in merged])  # index0=newest
        pipe.execute()
        written += 1
        if i % 25 == 0:
            print(f"[{i}/{len(universe)}] written={written} failed={len(failed)}")
        time.sleep(args.sleep_ms / 1000)

    args.backup.write_text(json.dumps(backup), encoding="utf-8")
    print(f"\n[APPLY] written={written} failed={len(failed)} backup={args.backup}")
    if failed:
        print(f"[APPLY] failed symbols ({len(failed)}): {failed}")
    return 0 if written else 2


if __name__ == "__main__":
    raise SystemExit(main())
