from __future__ import annotations

import argparse
import json
import os
import time

import redis

from runner.virtual_trade_ledger import VirtualTradeLedgerPublisher

_BOOK_PATTERN = "book:target:*-sleeve"
_CHECKPOINT_PREFIX = "shadow:ledger:checkpoint:"


def _latest_prices(
    mds_client,
    exchange: str,
    timeframe: str,
    symbols: list[str],
) -> tuple[dict[str, float], int]:
    prices: dict[str, float] = {}
    latest_open_ms = 0
    for symbol in symbols:
        raw = mds_client.lindex(
            f"kline_snapshot_v2:{exchange}:{timeframe}:{symbol}", 0
        )
        if not raw:
            continue
        row = json.loads(raw)
        price = float(row.get("close", 0.0))
        if price <= 0:
            continue
        prices[symbol] = price
        latest_open_ms = max(
            latest_open_ms,
            int(row.get("open_time", row.get("time", 0))),
        )
    return prices, latest_open_ms


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create one transparent mark-to-market checkpoint for sleeve ledgers."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--alpha", action="append", default=[])
    args = parser.parse_args()

    paper_url = os.environ.get("REDIS_URL", "")
    mds_url = os.environ.get("MDS_REDIS_URL", "")
    if not paper_url or not mds_url:
        raise SystemExit("REDIS_URL and MDS_REDIS_URL are required")
    paper = redis.from_url(paper_url, decode_responses=True)
    mds = redis.from_url(mds_url, decode_responses=True)
    selected = set(args.alpha)
    books = sorted(paper.scan_iter(match=_BOOK_PATTERN))
    checkpointed = 0
    for key in books:
        book = json.loads(paper.get(key))
        alpha_id = str(book["sleeve_id"])
        if selected and alpha_id not in selected:
            continue
        weights = {
            str(symbol): float(weight)
            for symbol, weight in dict(book["weights"]).items()
        }
        exchange = str((book.get("meta") or {}).get("exchange", "binance"))
        prices, latest_open_ms = _latest_prices(
            mds,
            exchange,
            str(book["timeframe"]),
            sorted(weights),
        )
        marker = f"{_CHECKPOINT_PREFIX}{alpha_id}:{latest_open_ms}"
        state_count = len(
            json.loads(
                paper.get(f"shadow:ledger:positions:{alpha_id}") or "{}"
            )
        )
        print(
            json.dumps(
                {
                    "alpha_id": alpha_id,
                    "weights": len(weights),
                    "price_coverage": len(prices),
                    "latest_open_ms": latest_open_ms,
                    "state_positions": state_count,
                    "already_checkpointed": bool(paper.exists(marker)),
                    "apply": args.apply,
                },
                sort_keys=True,
            )
        )
        if (
            not args.apply
            or not weights
            or len(prices) != len(weights)
            or latest_open_ms <= int(book["as_of_candle_ms"])
            or paper.exists(marker)
        ):
            continue
        ledger = VirtualTradeLedgerPublisher(
            paper,
            alpha_id,
            float((book.get("meta") or {}).get("capital", 10_000.0)),
            exchange,
        )
        ledger.rebalance(
            weights=weights,
            prices=prices,
            candle_open_ms=latest_open_ms,
            timeframe=str(book["timeframe"]),
            metadata_by_symbol={
                symbol: {
                    "virtual": True,
                    "checkpoint": True,
                    "checkpoint_source": "latest_confirmed_candle",
                }
                for symbol in weights
            },
            close_reason="VIRTUAL_LEDGER_CHECKPOINT",
        )
        paper.set(marker, str(int(time.time())), ex=90 * 86_400)
        checkpointed += 1
    print(json.dumps({"checkpointed": checkpointed, "apply": args.apply}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
