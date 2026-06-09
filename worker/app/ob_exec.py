from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


class ObExecCache:
    """Latest executable-side book prices per symbol from the ob_exec feed."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, float, str]] = {}  # symbol -> (bid, ask, state)

    def update(self, symbol: str, best_bid: float, best_ask: float, state: str) -> None:
        self._data[symbol] = (best_bid, best_ask, state)

    def side_price(self, symbol: str, position_side: str) -> float | None:
        item = self._data.get(symbol)
        if item is None:
            return None
        bid, ask, state = item
        if state != "READY":
            return None
        return bid if position_side.upper() == "LONG" else ask


def make_exit_price_fn(ob_cache: ObExecCache, ticker_cache):
    """Side-aware price provider: book best bid/ask when READY, else ticker mid."""

    def price_fn(symbol: str, position_side: str) -> float | None:
        book_price = ob_cache.side_price(symbol, position_side)
        if book_price is not None:
            return book_price
        return ticker_cache.get_prices([symbol]).get(symbol)

    return price_fn


async def run_ob_exec_subscriber(cache: ObExecCache, connect_redis, exchange: str = "binance") -> None:
    """Pattern-subscribe ob_exec:{exchange}:* and keep the cache fresh.

    MDS only publishes ob_exec for symbols it has WS depth for (i.e. the open-position
    symbols this worker subscribed), so the pattern naturally scopes to those.
    """
    redis_client = await connect_redis()
    pubsub = redis_client.pubsub()
    pattern = f"ob_exec:{exchange}:*"
    await pubsub.psubscribe(pattern)
    logger.info("[OB-EXEC] psubscribed %s", pattern)
    try:
        while True:
            try:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not msg or msg.get("type") != "pmessage":
                    continue
                data = json.loads(msg["data"])
                symbol = data.get("symbol")
                if not symbol:
                    continue
                cache.update(
                    symbol,
                    best_bid=float(data.get("best_bid", 0.0)),
                    best_ask=float(data.get("best_ask", 0.0)),
                    state=data.get("book_state", ""),
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[OB-EXEC] subscriber error: %s", exc)
                await asyncio.sleep(5)
    finally:
        await pubsub.punsubscribe()
        await pubsub.aclose()
        await redis_client.aclose()
