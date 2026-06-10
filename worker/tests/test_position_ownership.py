import json

import fakeredis.aioredis
import pytest

from app.db import Database
from app.position_ownership import PositionOwnershipMonitor


@pytest.mark.asyncio
async def test_exact_heartbeat_and_subscription_is_healthy(tmp_path):
    db = Database(str(tmp_path / "positions.db"))
    await db.init()
    await db.register_alpha("a")
    await db.create_position(
        "p1", "a", "s1", "BTCUSDT", "LONG", 100.0, 1.0,
        "2026-06-10T00:00:00Z",
    )
    paper = fakeredis.aioredis.FakeRedis(decode_responses=True)
    mds = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await paper.set("paper:alpha-runtime:a", json.dumps({
        "managed_position_ids": ["p1"],
        "desired_price_alert_symbols": ["BTCUSDT"],
        "runtime_state": "LIVE",
    }))
    await mds.sadd("price_alert:subscriptions:binance:a", "BTCUSDT")
    monitor = PositionOwnershipMonitor(db, paper, mds, grace_sec=0)
    assert (await monitor.check())["healthy"] is True
    await db.close()


@pytest.mark.asyncio
async def test_missing_owner_is_unhealthy_after_grace(tmp_path):
    db = Database(str(tmp_path / "positions.db"))
    await db.init()
    await db.register_alpha("a")
    await db.create_position("p1", "a", "s1", "BTCUSDT", "LONG", 100, 1,
                             "2026-06-10T00:00:00Z")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monitor = PositionOwnershipMonitor(db, redis, redis, grace_sec=0)
    report = await monitor.check()
    assert report["healthy"] is False
    assert report["details"][0]["missing_ids"] == ["p1"]
    await db.close()
