import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from base.config import BaseConfig
from base.engine import BaseEngine
from base.position_reconcile import normalize_position


class FakeRedis:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.values = {}

    def get(self, key):
        return json.dumps(self.snapshot)

    def set(self, key, value, ex=None):
        self.values[key] = (json.loads(value), ex)
        return True

    def close(self):
        pass


class Engine(BaseEngine):
    def __init__(self, config):
        super().__init__(config)
        self._open_positions = {"GHOST": {"position_id": "ghost"}}

    def get_required_channels(self): return []
    async def scan_loop(self): pass
    def _get_warmup_symbols(self): return []
    async def _manage_positions(self): pass
    def _has_open_positions(self): return bool(self._open_positions)


def test_normalize_position_restores_last_strategy_candle():
    position = normalize_position({
        "position_id": "p1",
        "alpha_id": "a",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": 100,
        "qty": 2,
        "tp": 110,
        "sl": 95,
        "metadata": {
            "strategy_runtime": {
                "entry_candle_open_ms": 1_000_000,
                "signal_candle_close_ms": 1_900_000,
                "last_strategy_candle_ms": 2_800_000,
            },
        },
    })

    assert position["last_strategy_candle_ms"] == 2_800_000


@pytest.mark.asyncio
async def test_reconcile_restores_authority_drops_ghost_and_heartbeats():
    config = MagicMock(spec=BaseConfig)
    config.ALPHA_ID = "a"
    config.REDIS_URL = "redis://paper"
    config.SYMBOL_BLACKLIST = ""
    config.POSITION_SNAPSHOT_MAX_AGE_SEC = 15
    config.ALPHA_RUNTIME_HEARTBEAT_TTL_SEC = 20
    engine = Engine(config)
    redis = FakeRedis({
        "alpha_id": "a", "revision": 7,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "positions": [{
            "position_id": "p1", "alpha_id": "a", "symbol": "BTCUSDT",
            "side": "LONG", "entry_price": 100, "qty": 2, "tp": 110, "sl": 95,
            "metadata": {},
        }],
    })
    assert await engine.reconcile_positions(redis) is True
    assert set(engine._open_positions) == {"BTCUSDT"}
    assert engine._open_positions["BTCUSDT"]["entry"] == 100
    heartbeat, ttl = redis.values["paper:alpha-runtime:a"]
    assert heartbeat["managed_position_ids"] == ["p1"]
    assert ttl == 20
