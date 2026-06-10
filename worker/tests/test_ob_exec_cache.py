import asyncio
import json

import fakeredis.aioredis
import pytest

from app.ob_exec import ObExecCache, make_exit_price_fn, run_ob_exec_subscriber


class _TickerCache:
    def __init__(self, prices):
        self._p = prices

    def get_price(self, symbol):
        return self._p.get(symbol)

    def get_quote(self, symbol):
        from app.ob_exec import PriceQuote

        price = self._p.get(symbol)
        return PriceQuote(price, "ticker_mid", False) if price is not None else None


def test_side_price_ready_long_uses_bid():
    c = ObExecCache()
    c.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="READY")
    assert c.side_price("BTCUSDT", "LONG") == 100.0
    assert c.side_price("BTCUSDT", "SHORT") == 101.0


def test_side_price_returns_none_when_not_ready():
    c = ObExecCache()
    c.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="STALE")
    assert c.side_price("BTCUSDT", "LONG") is None


def test_side_price_none_when_price_missing_or_zero():
    # A READY message with a missing/zero price must NOT be treated as a valid book
    # price (else side_price returns 0.0 and triggers a spurious TP/SL close at ~0).
    c = ObExecCache()
    c.update("BTCUSDT", best_bid=0.0, best_ask=101.0, state="READY")
    assert c.side_price("BTCUSDT", "LONG") is None     # bid unusable
    assert c.side_price("BTCUSDT", "SHORT") == 101.0   # ask still good


def test_side_price_none_when_stale():
    now = [1000.0]
    c = ObExecCache(staleness_sec=2.0, clock=lambda: now[0])
    c.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="READY")
    assert c.side_price("BTCUSDT", "LONG") == 100.0    # fresh
    now[0] += 5.0                                       # MDS stopped publishing
    assert c.side_price("BTCUSDT", "LONG") is None      # stale -> unavailable


def test_exit_price_fn_prefers_book_then_ticker():
    ob = ObExecCache()
    ob.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="READY")
    ticker = _TickerCache({"BTCUSDT": 100.5, "ETHUSDT": 3000.0})
    fn = make_exit_price_fn(ob, ticker)
    book_quote = fn("BTCUSDT", "LONG")
    ticker_quote = fn("ETHUSDT", "LONG")
    assert (book_quote.price, book_quote.source, book_quote.is_executable) == (
        100.0, "ob_exec", True)
    assert (ticker_quote.price, ticker_quote.source, ticker_quote.is_executable) == (
        3000.0, "ticker_mid", False)


@pytest.mark.asyncio
async def test_subscriber_updates_cache_from_pmessage():
    shared = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def connect_redis():
        return shared

    cache = ObExecCache()
    task = asyncio.create_task(run_ob_exec_subscriber(cache, connect_redis, "binance"))
    try:
        await asyncio.sleep(0.2)  # let psubscribe become active
        await shared.publish("ob_exec:binance:BTCUSDT", json.dumps(
            {"symbol": "BTCUSDT", "best_bid": 100.0, "best_ask": 101.0, "book_state": "READY"}))
        for _ in range(30):
            if cache.side_price("BTCUSDT", "LONG") is not None:
                break
            await asyncio.sleep(0.1)
        assert cache.side_price("BTCUSDT", "LONG") == 100.0
        assert cache.side_price("BTCUSDT", "SHORT") == 101.0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
