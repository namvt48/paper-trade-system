from __future__ import annotations

import json

import pytest
import redis as redis_lib

from runner.periodic_claim import claim_unclaimed, find_unclaimed_alphas


REDIS_URL = "redis://localhost:6382/15"


@pytest.fixture()
def r():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


def _seed_config(redis_client, alpha_id: str, tf_set: list[str] | None = None, enabled: bool = True):
    cfg = {"alpha_id": alpha_id, "tf_set": tf_set or ["1m"], "enabled": enabled}
    redis_client.set(f"runner:alpha:config:{alpha_id}", json.dumps(cfg))


def _seed_lease(redis_client, alpha_id: str, runner_id: str = "other-runner", ttl: int = 30):
    redis_client.set(f"runner:alpha:lease:{alpha_id}", runner_id, nx=True, ex=ttl)


def test_find_unclaimed_alphas(r):
    _seed_config(r, "a1")
    _seed_config(r, "a2")
    _seed_lease(r, "a1")
    result = find_unclaimed_alphas(r)
    assert len(result) == 1
    assert result[0]["alpha_id"] == "a2"


def test_find_unclaimed_alphas_all_claimed(r):
    _seed_config(r, "a1")
    _seed_lease(r, "a1")
    result = find_unclaimed_alphas(r)
    assert result == []


def test_claim_unclaimed_claims_available(r):
    _seed_config(r, "a1")
    _seed_config(r, "a2")
    _seed_lease(r, "a1")
    claimed = claim_unclaimed(r, "runner-1", ttl_sec=10)
    assert len(claimed) == 1
    assert claimed[0]["alpha_id"] == "a2"
    assert r.get("runner:alpha:lease:a2") == "runner-1"


def test_claim_unclaimed_none_available(r):
    _seed_config(r, "a1")
    _seed_lease(r, "a1")
    claimed = claim_unclaimed(r, "runner-1", ttl_sec=10)
    assert claimed == []
