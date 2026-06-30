from __future__ import annotations

import json

import pytest
import redis

from runner.config_listener import find_newly_disabled, find_newly_enabled


REDIS_URL = "redis://localhost:6382/15"


@pytest.fixture()
def r():
    client = redis.from_url(REDIS_URL, decode_responses=False)
    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip("paper-redis not available on localhost:6382")
    for key in client.scan_iter(match="runner:alpha:config:*"):
        client.delete(key)
    for key in client.scan_iter(match="runner:alpha:lease:*"):
        client.delete(key)
    yield client
    for key in client.scan_iter(match="runner:alpha:config:*"):
        client.delete(key)
    for key in client.scan_iter(match="runner:alpha:lease:*"):
        client.delete(key)
    client.close()


def test_find_newly_disabled(r):
    a1_cfg = json.dumps({"alpha_id": "a1", "enabled": True, "strategy": "s1"})
    a3_cfg = json.dumps({"alpha_id": "a3", "enabled": False, "strategy": "s3"})
    r.set("runner:alpha:config:a1", a1_cfg)
    r.set("runner:alpha:config:a3", a3_cfg)

    currently_owned = ["a1", "a2", "a3"]
    disabled = find_newly_disabled(r, currently_owned)

    assert "a2" in disabled
    assert "a3" in disabled
    assert "a1" not in disabled


def test_find_newly_enabled(r):
    a1_cfg = json.dumps({"alpha_id": "a1", "enabled": True, "strategy": "s1", "tf_set": ["1m"]})
    a2_cfg = json.dumps({"alpha_id": "a2", "enabled": True, "strategy": "s2", "tf_set": ["5m"]})
    r.set("runner:alpha:config:a1", a1_cfg)
    r.set("runner:alpha:config:a2", a2_cfg)

    r.set("runner:alpha:lease:a1", "other-runner", ex=30)

    currently_owned = ["a1"]
    newly_enabled = find_newly_enabled(r, currently_owned)

    assert len(newly_enabled) == 1
    assert newly_enabled[0]["alpha_id"] == "a2"
