from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceQuote:
    price: float
    source: str
    is_executable: bool


class ObExecCache:
    """Latest executable-side book prices per symbol from the ob_exec feed.

    A book price is only served when the last message was READY, fresh (within
    ``staleness_sec``), and carries a usable (>0) price — otherwise callers fall back
    to the ticker. This guards against a frozen READY quote after MDS stops publishing
    and against a malformed message whose price defaulted to 0.
    """

    def __init__(self, staleness_sec: float = 2.0, clock=time.monotonic) -> None:
        # symbol -> (bid, ask, state, last_update_ts)
        self._data: dict[str, tuple[float, float, str, float]] = {}
        self._staleness_sec = staleness_sec
        self._clock = clock

    def update(self, symbol: str, best_bid: float, best_ask: float, state: str) -> None:
        self._data[symbol] = (best_bid, best_ask, state, self._clock())

    def side_price(self, symbol: str, position_side: str) -> float | None:
        quote = self.side_quote(symbol, position_side)
        return quote.price if quote is not None else None

    def side_quote(self, symbol: str, position_side: str) -> PriceQuote | None:
        item = self._data.get(symbol)
        if item is None:
            return None
        bid, ask, state, ts = item
        if state != "READY":
            return None
        if self._clock() - ts > self._staleness_sec:
            return None  # MDS stopped publishing; don't serve a frozen quote
        price = bid if position_side.upper() == "LONG" else ask
        if not price or price <= 0:
            return None  # missing/zero price is not a usable executable price
        return PriceQuote(price=price, source="ob_exec", is_executable=True)


def make_exit_price_fn(ob_cache: ObExecCache, ticker_cache):
    """Side-aware price provider: book best bid/ask when READY, else ticker mid."""

    def price_fn(symbol: str, position_side: str) -> float | None:
        quote = ob_cache.side_quote(symbol, position_side)
        if quote is not None:
            return quote
        return ticker_cache.get_quote(symbol)

    return price_fn


async def run_ob_exec_subscriber(cache: ObExecCache, connect_redis, exchange: str = "binance") -> None:
    """Pattern-subscribe ob_exec:{exchange}:* and keep the cache fresh.

    MDS only publishes ob_exec for symbols it has WS depth for (i.e. the open-position
    symbols this worker subscribed), so the pattern naturally scopes to those.
    """
    pattern = f"ob_exec:{exchange}:*"
    while True:
        redis_client = None
        pubsub = None
        try:
            redis_client = await connect_redis()
            pubsub = redis_client.pubsub()
            await pubsub.psubscribe(pattern)
            logger.info("[OB-EXEC] psubscribed %s", pattern)
            while True:
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
            if pubsub is not None:
                await pubsub.punsubscribe()
                await pubsub.aclose()
            if redis_client is not None:
                await redis_client.aclose()
