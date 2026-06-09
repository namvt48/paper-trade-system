import json

import fakeredis.aioredis
import pytest

from app.slippage_client import SlippageClient, FillService, order_side_for


def test_order_side_mapping():
    assert order_side_for("LONG", is_close=False) == "BUY"
    assert order_side_for("SHORT", is_close=False) == "SELL"
    assert order_side_for("LONG", is_close=True) == "SELL"
    assert order_side_for("SHORT", is_close=True) == "BUY"


@pytest.mark.asyncio
async def test_query_returns_response_when_present():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = SlippageClient(r)
    # Pre-seed the response a server would have written.
    await r.lpush("orderbook:slip:resp:rid-1", json.dumps({"avg_exec_price": 101.0, "fallback_used": False}))
    resp = await client.query("binance", "BTCUSDT", "BUY", 1.0, fallback_pct=0.5,
                              timeout=1, request_id="rid-1")
    assert resp["avg_exec_price"] == 101.0
    # The request was enqueued for the server.
    raw = await r.lpop("orderbook:slip:req:binance")
    assert json.loads(raw)["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_query_returns_none_on_timeout():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = SlippageClient(r)
    resp = await client.query("binance", "BTCUSDT", "BUY", 1.0, timeout=1, request_id="rid-x")
    assert resp is None


@pytest.mark.asyncio
async def test_fill_service_uses_book_avg():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.lpush("orderbook:slip:resp:rid-2", json.dumps(
        {"fallback_used": False, "filled_qty": 1.0, "requested_qty": 1.0, "avg_exec_price": 101.0}))
    svc = FillService(SlippageClient(r), slippage_pct=0.5, timeout=1)
    price = await svc.resolve("binance", "BTCUSDT", "LONG", 1.0, ref_price=100.0,
                              is_close=False, request_id="rid-2")
    assert price == 101.0


@pytest.mark.asyncio
async def test_fill_service_falls_back_on_timeout():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    svc = FillService(SlippageClient(r), slippage_pct=0.5, timeout=1)
    # no response seeded -> timeout -> fixed-pct (LONG open ref 100 pct 0.5 -> 100.05)
    price = await svc.resolve("binance", "BTCUSDT", "LONG", 1.0, ref_price=100.0,
                              is_close=False, request_id="rid-y")
    assert price == 100.05
