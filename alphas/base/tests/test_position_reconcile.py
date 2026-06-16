import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from base.config import BaseConfig
from base.engine import BaseEngine
from base.position_reconcile import normalize_position


_MISSING = object()


class FakeRedis:
    def __init__(self, snapshot=None, raw_snapshot=_MISSING):
        self.snapshot = snapshot
        self.raw_snapshot = raw_snapshot
        self.values = {}

    def get(self, key):
        if self.raw_snapshot is not _MISSING:
            return self.raw_snapshot
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


def make_config():
    config = MagicMock(spec=BaseConfig)
    config.ALPHA_ID = "a"
    config.REDIS_URL = "redis://paper"
    config.SYMBOL_BLACKLIST = ""
    config.POSITION_SNAPSHOT_MAX_AGE_SEC = 15
    config.ALPHA_RUNTIME_HEARTBEAT_TTL_SEC = 20
    config.RECONCILE_NO_POSITION_IS_OK = True
    config.RECONCILE_STALE_SUSPEND_NEW_ENTRIES = True
    config.DATA_STALE_SUSPEND_NEW_ENTRIES = True
    config.PRICE_ALERT_SYNC_SUSPEND_NEW_ENTRIES = True
    return config


@pytest.fixture
def engine_with_config():
    return Engine(make_config())


def fresh_empty_snapshot():
    return {
        "alpha_id": "a",
        "revision": 8,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "positions": [],
    }


def snapshot_with_one_stale_position():
    return {
        "alpha_id": "a",
        "revision": 9,
        "generated_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
        "positions": [{
            "position_id": "p1", "alpha_id": "a", "symbol": "BTCUSDT",
            "side": "LONG", "entry_price": 100, "qty": 2, "tp": 110, "sl": 95,
            "metadata": {},
        }],
    }


@pytest.mark.asyncio
async def test_reconcile_restores_authority_drops_ghost_and_heartbeats():
    config = make_config()
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


@pytest.mark.asyncio
async def test_reconcile_missing_snapshot_no_local_positions_is_ok(engine_with_config):
    engine = engine_with_config
    engine._open_positions = {}
    redis = FakeRedis(raw_snapshot=None)
    assert await engine.reconcile_positions(redis) is True
    assert engine._position_reconcile_stale is False
    assert engine.runtime_state != "STALE"
    assert engine._last_reconcile_at > 0


@pytest.mark.asyncio
async def test_reconcile_stale_snapshot_with_positions_suspends_only_reconcile(engine_with_config):
    engine = engine_with_config
    engine.runtime_state = "LIVE"
    redis = FakeRedis(snapshot_with_one_stale_position())
    assert await engine.reconcile_positions(redis) is True
    assert engine._position_reconcile_stale is True
    assert engine._data_stale is False
    assert engine.can_open_new_trades() is False


@pytest.mark.asyncio
async def test_reconcile_recovery_does_not_force_live_when_data_stale(engine_with_config):
    engine = engine_with_config
    engine.runtime_state = "STALE"
    engine._data_stale = True
    engine._position_reconcile_stale = True
    redis = FakeRedis(fresh_empty_snapshot())
    assert await engine.reconcile_positions(redis) is True
    assert engine._position_reconcile_stale is False
    assert engine._data_stale is True
    assert engine.runtime_state == "STALE"
