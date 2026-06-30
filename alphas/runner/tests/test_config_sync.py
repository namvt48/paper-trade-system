from __future__ import annotations

import json

import pytest
import redis

from runner.config import AlphaConfig
from runner.config_sync import (
    ALPHA_CONFIG_PREFIX,
    CONFIG_UPDATED_CHANNEL,
    read_alpha_configs_from_redis,
    sync_config_to_redis,
)
from runner.strategy.base import Strategy
from runner.strategy.registry import StrategyRegistry


REDIS_URL = "redis://localhost:6382/15"


class SyncTestStrategy(Strategy):
    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        tfs = params.get("tfs", ["15m"])
        return [f"kline:binance:{tf}" for tf in tfs]

    def get_required_channels_instance(self): return []
    def get_warmup_symbols(self): return []
    def get_warmup_tfs(self): return []
    def get_warmup_bars(self, tf): return 0


@pytest.fixture()
def r():
    client = redis.from_url(REDIS_URL, decode_responses=False)
    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip("paper-redis not available on localhost:6382")
    for key in client.scan_iter(match=f"{ALPHA_CONFIG_PREFIX}*"):
        client.delete(key)
    yield client
    for key in client.scan_iter(match=f"{ALPHA_CONFIG_PREFIX}*"):
        client.delete(key)


def _write_yaml(path, text: str):
    path.write_text(text, encoding="utf-8")


def _register_strategy(monkeypatch):
    registry = StrategyRegistry()
    registry.register("test_strat", SyncTestStrategy)

    def _fake_build(cfg):
        return registry

    import runner.config_sync as cs
    monkeypatch.setattr(cs, "_build_registry", _fake_build)


def test_sync_writes_enabled_alpha_to_redis(tmp_path, r, monkeypatch):
    _register_strategy(monkeypatch)
    cfg_path = tmp_path / "runner.yaml"
    _write_yaml(
        cfg_path,
        """
runner_id: test-runner
shadow_mode: true
alphas:
  - alpha_id: a1
    strategy: test_strat
    enabled: true
    params: {tfs: [15m, 1h]}
""",
    )

    count = sync_config_to_redis(str(cfg_path), r)

    assert count == 1
    raw = r.get(f"{ALPHA_CONFIG_PREFIX}a1")
    blob = json.loads(raw)
    assert blob["alpha_id"] == "a1"
    assert blob["strategy"] == "test_strat"
    assert blob["enabled"] is True
    assert blob["required_channels"] == ["kline:binance:15m", "kline:binance:1h"]


def test_sync_skips_disabled_alpha(tmp_path, r, monkeypatch):
    _register_strategy(monkeypatch)
    cfg_path = tmp_path / "runner.yaml"
    _write_yaml(
        cfg_path,
        """
runner_id: test-runner
shadow_mode: true
alphas:
  - alpha_id: a1
    strategy: test_strat
    enabled: true
    params: {}
  - alpha_id: a2
    strategy: test_strat
    enabled: false
    params: {}
""",
    )

    count = sync_config_to_redis(str(cfg_path), r)

    assert count == 1
    assert r.exists(f"{ALPHA_CONFIG_PREFIX}a1")
    assert not r.exists(f"{ALPHA_CONFIG_PREFIX}a2")


def test_sync_deletes_stale_keys(tmp_path, r, monkeypatch):
    _register_strategy(monkeypatch)
    r.set(f"{ALPHA_CONFIG_PREFIX}stale_alpha", json.dumps({"alpha_id": "stale_alpha", "enabled": True}))

    cfg_path = tmp_path / "runner.yaml"
    _write_yaml(
        cfg_path,
        """
runner_id: test-runner
shadow_mode: true
alphas:
  - alpha_id: a1
    strategy: test_strat
    enabled: true
    params: {}
""",
    )

    sync_config_to_redis(str(cfg_path), r)

    assert not r.exists(f"{ALPHA_CONFIG_PREFIX}stale_alpha")
    assert r.exists(f"{ALPHA_CONFIG_PREFIX}a1")


def test_sync_publishes_config_updated(tmp_path, r, monkeypatch):
    _register_strategy(monkeypatch)
    pubsub = r.pubsub()
    pubsub.subscribe(CONFIG_UPDATED_CHANNEL)

    cfg_path = tmp_path / "runner.yaml"
    _write_yaml(
        cfg_path,
        """
runner_id: test-runner
shadow_mode: true
alphas:
  - alpha_id: a1
    strategy: test_strat
    enabled: true
    params: {}
""",
    )

    sync_config_to_redis(str(cfg_path), r)

    msg = pubsub.get_message(timeout=2.0)
    while msg is not None and msg["type"] != "message":
        msg = pubsub.get_message(timeout=2.0)
    assert msg is not None and msg["type"] == "message"
    payload = json.loads(msg["data"])
    assert payload["runner_id"] == "test-runner"
    pubsub.unsubscribe()


def test_read_alpha_configs_from_redis(tmp_path, r, monkeypatch):
    _register_strategy(monkeypatch)
    cfg_path = tmp_path / "runner.yaml"
    _write_yaml(
        cfg_path,
        """
runner_id: test-runner
shadow_mode: true
alphas:
  - alpha_id: a1
    strategy: test_strat
    enabled: true
    params: {tfs: [4h]}
  - alpha_id: a2
    strategy: test_strat
    enabled: true
    params: {tfs: [1d]}
""",
    )

    sync_config_to_redis(str(cfg_path), r)
    configs = read_alpha_configs_from_redis(r)

    ids = {c["alpha_id"] for c in configs}
    assert ids == {"a1", "a2"}


def test_tf_set_derived_from_classmethod(tmp_path, r, monkeypatch):
    _register_strategy(monkeypatch)
    cfg_path = tmp_path / "runner.yaml"
    _write_yaml(
        cfg_path,
        """
runner_id: test-runner
shadow_mode: true
alphas:
  - alpha_id: a1
    strategy: test_strat
    enabled: true
    params: {tfs: [15m, 1h, 4h]}
""",
    )

    sync_config_to_redis(str(cfg_path), r)

    raw = r.get(f"{ALPHA_CONFIG_PREFIX}a1")
    blob = json.loads(raw)
    assert blob["tf_set"] == ["15m", "1h", "4h"]
