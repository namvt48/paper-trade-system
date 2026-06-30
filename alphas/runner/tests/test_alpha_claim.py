from __future__ import annotations

import pytest
import redis as redis_lib

from runner.alpha_claim import AlphaGroup, claim_alpha_group, claim_alpha_groups, group_alphas_by_tf_set


REDIS_URL = "redis://localhost:6382/15"


@pytest.fixture()
def r():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


def _cfg(alpha_id: str, tf_set: list[str]) -> dict:
    return {"alpha_id": alpha_id, "tf_set": tf_set}


def test_group_alphas_by_tf_set():
    configs = [
        _cfg("a1", ["1m", "5m"]),
        _cfg("a2", ["1m", "5m"]),
        _cfg("a3", ["15m"]),
        _cfg("a4", ["15m"]),
    ]
    groups = group_alphas_by_tf_set(configs)
    assert len(groups) == 2
    by_key = {g.tf_key: g for g in groups}
    assert "1m,5m" in by_key
    assert "15m" in by_key
    assert len(by_key["1m,5m"].alpha_ids) == 2
    assert len(by_key["15m"].alpha_ids) == 2


def test_group_split_on_max_alphas():
    configs = [_cfg(f"a{i}", ["1m"]) for i in range(25)]
    groups = group_alphas_by_tf_set(configs, max_alphas_per_runner=10)
    assert len(groups) == 3
    sizes = sorted([len(g.alpha_ids) for g in groups])
    assert sizes == [5, 10, 10]


def test_claim_alpha_group_success(r):
    group = AlphaGroup(
        tf_key="1m,5m",
        tf_set=["1m", "5m"],
        alpha_ids=["a1", "a2"],
        configs=[_cfg("a1", ["1m", "5m"]), _cfg("a2", ["1m", "5m"])],
    )
    ok = claim_alpha_group(r, "runner-1", group, ttl_sec=10)
    assert ok is True
    assert r.get("runner:alpha:lease:a1") == "runner-1"
    assert r.get("runner:alpha:lease:a2") == "runner-1"


def test_claim_alpha_group_partial_fail_rolls_back(r):
    r.set("runner:alpha:lease:a2", "other-runner", nx=True, ex=30)
    group = AlphaGroup(
        tf_key="1m",
        tf_set=["1m"],
        alpha_ids=["a1", "a2"],
        configs=[_cfg("a1", ["1m"]), _cfg("a2", ["1m"])],
    )
    ok = claim_alpha_group(r, "runner-1", group, ttl_sec=10)
    assert ok is False
    assert r.get("runner:alpha:lease:a1") is None


def test_claim_alpha_group_reclaims_same_runner_lease(r):
    r.set("runner:alpha:lease:a1", "runner-1", ex=30)
    group = AlphaGroup(
        tf_key="1m",
        tf_set=["1m"],
        alpha_ids=["a1", "a2"],
        configs=[_cfg("a1", ["1m"]), _cfg("a2", ["1m"])],
    )
    ok = claim_alpha_group(r, "runner-1", group, ttl_sec=10)
    assert ok is True
    assert r.get("runner:alpha:lease:a1") == "runner-1"
    assert r.get("runner:alpha:lease:a2") == "runner-1"
    assert 0 < r.ttl("runner:alpha:lease:a1") <= 10


def test_claim_alpha_groups_greedy(r):
    configs = [
        _cfg("a1", ["1m"]),
        _cfg("a2", ["5m"]),
        _cfg("a3", ["1m"]),
    ]
    claimed = claim_alpha_groups(r, "runner-1", configs, ttl_sec=10)
    assert len(claimed) == 3
    assert r.get("runner:alpha:lease:a1") == "runner-1"
    assert r.get("runner:alpha:lease:a2") == "runner-1"
    assert r.get("runner:alpha:lease:a3") == "runner-1"


def test_claim_skips_already_leased_group_rolls_back(r):
    r.set("runner:alpha:lease:a1", "other-runner", nx=True, ex=30)
    configs = [_cfg("a1", ["1m"]), _cfg("a2", ["1m"])]
    claimed = claim_alpha_groups(r, "runner-1", configs, ttl_sec=10)
    assert claimed == []
    assert r.get("runner:alpha:lease:a2") is None


def test_claim_different_groups_independent(r):
    r.set("runner:alpha:lease:a1", "other-runner", nx=True, ex=30)
    configs = [_cfg("a1", ["1m"]), _cfg("a2", ["15m"])]
    claimed = claim_alpha_groups(r, "runner-1", configs, ttl_sec=10)
    assert len(claimed) == 1
    assert claimed[0]["alpha_id"] == "a2"
    assert r.get("runner:alpha:lease:a2") == "runner-1"
