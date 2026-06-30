from __future__ import annotations

import json
import pytest
import redis as redislib

from runner.alpha_claim import claim_alpha_groups, group_alphas_by_tf_set
from runner.config_sync import sync_config_to_redis, read_alpha_configs_from_redis
from runner.periodic_claim import find_unclaimed_alphas, claim_unclaimed
from runner.config_listener import find_newly_disabled, find_newly_enabled
from runner.strategy.base import Strategy
from runner.strategy.registry import StrategyRegistry


REDIS_DB = 15
REDIS_URL = f"redis://localhost:6382/{REDIS_DB}"


class _MockV5Tail(Strategy):
    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        tf = params.get("tf", "1m")
        return [f"kline:{tf}"]

    def get_required_channels_instance(self): return []
    def get_warmup_symbols(self): return []
    def get_warmup_tfs(self): return []
    def get_warmup_bars(self, tf): return 0


class _MockHyperTurboV2(Strategy):
    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        tf = params.get("tf", "4h")
        return [f"kline:{tf}", "kline:1m"]

    def get_required_channels_instance(self): return []
    def get_warmup_symbols(self): return []
    def get_warmup_tfs(self): return []
    def get_warmup_bars(self, tf): return 0


def _mock_registry():
    registry = StrategyRegistry()
    registry.register("v5_tail", _MockV5Tail)
    registry.register("hyper_turbo_v2", _MockHyperTurboV2)
    return registry


@pytest.fixture
def redis_client():
    r = redislib.Redis(host="localhost", port=6382, decode_responses=True, db=REDIS_DB)
    try:
        r.ping()
    except redislib.ConnectionError:
        pytest.skip("paper-redis not available on localhost:6382")
    r.flushdb()
    yield r
    r.flushdb()
    r.close()


@pytest.fixture
def multi_yaml_path(tmp_path, monkeypatch):
    import runner.config_sync as cs
    monkeypatch.setattr(cs, "_build_registry", lambda cfg: _mock_registry())

    p = tmp_path / "runner-config.yaml"
    p.write_text("""
runner:
  runner_id: integration-runner
  redis_url: redis://paper-redis:6379
  mds_redis_url: redis://mds-redis:6379
  mds_exchange: binance
  signal_stream: paper-signals-shadow
  shadow_mode: true
modules:
  - runner.strategies.v5_tail
  - runner.strategies.hyper_turbo_v2
alphas:
  - alpha_id: v5-1m-1
    strategy: v5_tail
    version: "1"
    enabled: true
    params: {tf: "1m"}
  - alpha_id: v5-1m-2
    strategy: v5_tail
    version: "1"
    enabled: true
    params: {tf: "1m"}
  - alpha_id: v5-1m-3
    strategy: v5_tail
    version: "1"
    enabled: true
    params: {tf: "1m"}
  - alpha_id: ht-4h-1
    strategy: hyper_turbo_v2
    version: "1"
    enabled: true
    params: {tf: "4h"}
  - alpha_id: ht-4h-2
    strategy: hyper_turbo_v2
    version: "1"
    enabled: true
    params: {tf: "4h"}
""")
    return str(p)


def test_sync_then_two_runners_claim(redis_client, multi_yaml_path):
    sync_config_to_redis(multi_yaml_path, redis_client)
    configs = read_alpha_configs_from_redis(redis_client)
    assert len(configs) == 5

    runner_a = claim_alpha_groups(redis_client, "runner-a", configs, max_alphas_per_runner=10)
    assert len(runner_a) == 5

    remaining = [c for c in configs if c["alpha_id"] not in {x["alpha_id"] for x in runner_a}]
    runner_b = claim_alpha_groups(redis_client, "runner-b", remaining, max_alphas_per_runner=10)
    assert len(runner_b) == 0


def test_sync_then_runner_crash_reclaim(redis_client, multi_yaml_path):
    sync_config_to_redis(multi_yaml_path, redis_client)
    configs = read_alpha_configs_from_redis(redis_client)

    runner_a = claim_alpha_groups(redis_client, "runner-a", configs, max_alphas_per_runner=10)
    assert len(runner_a) == 5

    for c in runner_a:
        key = f"runner:alpha:lease:{c['alpha_id']}"
        redis_client.delete(key)

    runner_b = claim_alpha_groups(redis_client, "runner-b", configs, max_alphas_per_runner=10)
    assert len(runner_b) == 5


def test_sync_tfs_grouped_correctly(redis_client, multi_yaml_path):
    sync_config_to_redis(multi_yaml_path, redis_client)
    configs = read_alpha_configs_from_redis(redis_client)

    groups = group_alphas_by_tf_set(configs, max_alphas_per_runner=10)
    tf_keys = [g.tf_key for g in groups]
    assert "1m" in tf_keys
    assert "1m,4h" in tf_keys

    one_m_group = [g for g in groups if g.tf_key == "1m"][0]
    assert len(one_m_group.alpha_ids) == 3

    ht_group = [g for g in groups if g.tf_key == "1m,4h"][0]
    assert len(ht_group.alpha_ids) == 2


def test_periodic_claim_finds_unclaimed(redis_client, multi_yaml_path):
    sync_config_to_redis(multi_yaml_path, redis_client)
    configs = read_alpha_configs_from_redis(redis_client)

    for c in configs[:2]:
        redis_client.set(f"runner:alpha:lease:{c['alpha_id']}", "runner-a", ex=20)

    unclaimed = find_unclaimed_alphas(redis_client)
    assert len(unclaimed) >= 2

    runner_b_new = claim_unclaimed(redis_client, "runner-b", max_alphas_per_runner=10)
    assert len(runner_b_new) >= 2


def test_config_listener_detects_disabled(redis_client, multi_yaml_path):
    sync_config_to_redis(multi_yaml_path, redis_client)
    configs = read_alpha_configs_from_redis(redis_client)

    runner_a = claim_alpha_groups(redis_client, "runner-a", configs, max_alphas_per_runner=10)
    owned = [c["alpha_id"] for c in runner_a]

    redis_client.delete(f"runner:alpha:config:{owned[0]}")
    disabled = find_newly_disabled(redis_client, owned)
    assert owned[0] in disabled


def test_config_listener_detects_newly_enabled(redis_client, multi_yaml_path):
    sync_config_to_redis(multi_yaml_path, redis_client)
    configs = read_alpha_configs_from_redis(redis_client)

    runner_a = claim_alpha_groups(redis_client, "runner-a", configs, max_alphas_per_runner=10)
    owned = [c["alpha_id"] for c in runner_a]

    redis_client.set("runner:alpha:config:new-alpha", json.dumps({
        "alpha_id": "new-alpha", "enabled": True, "tf_set": ["1m"],
        "strategy": "v5_tail", "params": {"tf": "1m"},
    }))

    new_enabled = find_newly_enabled(redis_client, owned)
    assert len(new_enabled) == 1
    assert new_enabled[0]["alpha_id"] == "new-alpha"
