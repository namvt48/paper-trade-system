import json

import fakeredis.aioredis
import pytest

from app.db import Database
from app.position_snapshots import PositionSnapshotPublisher


@pytest.mark.asyncio
async def test_snapshot_is_per_alpha_and_publishes_empty_after_close(tmp_path):
    db = Database(str(tmp_path / "positions.db"))
    await db.init()
    for alpha, position, symbol in (("a", "p1", "BTCUSDT"), ("b", "p2", "ETHUSDT")):
        await db.register_alpha(alpha)
        await db.create_position(
            position, alpha, f"s-{position}", symbol, "LONG", 100.0, 1.0,
            "2026-06-10T00:00:00Z", metadata='{"strategy_runtime":{"hse":101}}',
        )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    publisher = PositionSnapshotPublisher(db, redis)
    payload = await publisher.publish_alpha("a")
    assert [row["position_id"] for row in payload["positions"]] == ["p1"]
    assert payload["positions"][0]["metadata"]["strategy_runtime"]["hse"] == 101

    await db.close_position("p1", 101.0, "DONE", "2026-06-10T01:00:00Z")
    payload = await publisher.publish_alpha("a")
    assert payload["positions"] == []
    assert json.loads(await redis.get("paper:positions:snapshot:a"))["revision"] == 2
    await db.close()
