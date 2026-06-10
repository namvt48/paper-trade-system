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
async def test_breaker_short_circuits_after_consecutive_failures():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = SlippageClient(r, failure_threshold=2, cooldown_sec=100.0)
    # two timeouts (no response seeded) -> breaker opens
    assert await client.query("binance", "BTCUSDT", "BUY", 1.0, timeout=0.05, request_id="a") is None
    assert await client.query("binance", "BTCUSDT", "BUY", 1.0, timeout=0.05, request_id="b") is None
    # breaker now open: next call returns None immediately WITHOUT pushing a request
    await r.delete("orderbook:slip:req:binance")
    assert await client.query("binance", "BTCUSDT", "BUY", 1.0, timeout=0.05, request_id="c") is None
    assert await r.llen("orderbook:slip:req:binance") == 0


@pytest.mark.asyncio
async def test_request_list_is_capped():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = SlippageClient(r, max_req_backlog=3, failure_threshold=100)  # breaker won't open
    for i in range(6):
        await client.query("binance", "BTCUSDT", "BUY", 1.0, timeout=0.02, request_id=f"r{i}")
    assert await r.llen("orderbook:slip:req:binance") <= 3


@pytest.mark.asyncio
async def test_fill_service_falls_back_on_timeout():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    svc = FillService(SlippageClient(r), slippage_pct=0.5, timeout=1)
    # no response seeded -> timeout -> fixed-pct (LONG open ref 100 pct 0.5 -> 100.05)
    price = await svc.resolve("binance", "BTCUSDT", "LONG", 1.0, ref_price=100.0,
                              is_close=False, request_id="rid-y")
    assert price == 100.05


@pytest.mark.asyncio
async def test_fill_service_skips_rpc_for_unsupported_exchange():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    svc = FillService(
        SlippageClient(r), slippage_pct=0.5, timeout=0.01,
        supported_exchanges={"binance"},
    )
    price = await svc.resolve(
        "okx", "BTCUSDT", "LONG", 1.0, ref_price=100.0,
        is_close=False, request_id="unsupported",
    )
    assert price == 100.05
    assert await r.llen("orderbook:slip:req:okx") == 0


@pytest.mark.asyncio
async def test_query_rejects_mismatched_response_id():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.lpush("orderbook:slip:resp:expected", json.dumps({
        "request_id": "wrong",
        "fallback_used": False,
        "filled_qty": 1.0,
        "requested_qty": 1.0,
        "avg_exec_price": 101.0,
    }))
    client = SlippageClient(r)
    assert await client.query(
        "binance", "BTCUSDT", "BUY", 1.0, timeout=1, request_id="expected",
    ) is None
