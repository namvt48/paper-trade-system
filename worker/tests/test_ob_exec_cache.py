import asyncio
import json

import fakeredis.aioredis
import pytest

from app.ob_exec import ObExecCache, make_exit_price_fn, run_ob_exec_subscriber


class _TickerCache:
    def __init__(self, prices):
        self._p = prices

    def get_prices(self, symbols=None):
        if symbols is None:
            return dict(self._p)
        return {s: self._p[s] for s in symbols if s in self._p}


def test_side_price_ready_long_uses_bid():
    c = ObExecCache()
    c.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="READY")
    assert c.side_price("BTCUSDT", "LONG") == 100.0
    assert c.side_price("BTCUSDT", "SHORT") == 101.0


def test_side_price_returns_none_when_not_ready():
    c = ObExecCache()
    c.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="STALE")
    assert c.side_price("BTCUSDT", "LONG") is None


def test_exit_price_fn_prefers_book_then_ticker():
    ob = ObExecCache()
    ob.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="READY")
    ticker = _TickerCache({"BTCUSDT": 100.5, "ETHUSDT": 3000.0})
    fn = make_exit_price_fn(ob, ticker)
    assert fn("BTCUSDT", "LONG") == 100.0       # book best_bid
    assert fn("ETHUSDT", "LONG") == 3000.0      # ticker fallback (no book)


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
