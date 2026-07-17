from __future__ import annotations

import asyncio
import json

import pytest
import redis

from runner import config_listener
from runner.config_listener import find_newly_disabled, find_newly_enabled, run_config_listener


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


class FakeAsyncPubSub:
    def __init__(self, messages):
        self._messages = list(messages)
        self.subscribed: list[str] = []

    async def subscribe(self, channel):
        self.subscribed.append(channel)

    async def get_message(self, ignore_subscribe_messages=True, timeout=0.05):
        if self._messages:
            return self._messages.pop(0)
        return None

    async def unsubscribe(self):
        pass

    async def close(self):
        pass


class FakeAsyncRedis:
    def __init__(self, pubsub_obj):
        self._pubsub_obj = pubsub_obj
        self.closed = False

    def pubsub(self):
        return self._pubsub_obj

    async def close(self):
        self.closed = True


class FakeConnectionPool:
    connection_kwargs = {"host": "localhost", "port": 6382, "db": 15}


class FakeSyncRedisForUrl:
    connection_pool = FakeConnectionPool()


@pytest.mark.asyncio
async def test_run_config_listener_uses_async_pubsub_not_thread_pool(monkeypatch):
    """Regression: the config-update listener previously polled a sync
    pubsub via loop.run_in_executor(None, pubsub.get_message, 1.0) in an
    infinite loop -- permanently occupying one slot of the runner's
    shared compute thread pool for the process's entire lifetime,
    competing with alpha scan() work (2026-07-16 incident, see
    .agents/PLAN.md U3). It must use its own async connection instead,
    consuming no thread-pool capacity for the polling itself.
    """
    message = {"type": "message", "channel": "runner:config:updated", "data": b"1"}
    fake_pubsub = FakeAsyncPubSub([message])
    fake_async_redis = FakeAsyncRedis(fake_pubsub)
    captured_urls = []

    def fake_from_url(url, decode_responses=False):
        captured_urls.append(url)
        return fake_async_redis

    monkeypatch.setattr(config_listener.aioredis, "from_url", fake_from_url)
    monkeypatch.setattr(config_listener, "find_newly_disabled", lambda *a, **k: ["a2"])
    monkeypatch.setattr(config_listener, "find_newly_enabled", lambda *a, **k: [])

    disabled_calls = []
    stop = asyncio.Event()

    async def on_disabled(ids):
        disabled_calls.append(ids)
        stop.set()

    task = asyncio.create_task(run_config_listener(
        FakeSyncRedisForUrl(),
        "runner-1",
        currently_owned_fn=lambda: ["a1", "a2"],
        on_disabled=on_disabled,
        stop_event=stop,
    ))

    await asyncio.wait_for(stop.wait(), timeout=1.0)
    await asyncio.wait_for(task, timeout=1.0)

    assert disabled_calls == [["a2"]]
    assert captured_urls == ["redis://localhost:6382/15"]
    assert fake_pubsub.subscribed == ["runner:config:updated"]
    assert fake_async_redis.closed
