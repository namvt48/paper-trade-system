from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from base.json_utils import loads as json_loads
from runner.data_layer.cache import SharedCandleCache


@dataclass(frozen=True)
class DataEvent:
    channel: str
    kind: str
    symbol: str = ""
    tf: str = ""
    payload: dict | None = None


class SharedPubSubManager:
    def __init__(self, redis_client, cache: SharedCandleCache, queue_maxsize: int = 1000):
        self.redis_client = redis_client
        self.pubsub = redis_client.pubsub()
        self.cache = cache
        self.queue_maxsize = int(queue_maxsize)
        self._subscribers: dict[str, set[str]] = defaultdict(set)
        self._strategy_channels: dict[str, set[str]] = defaultdict(set)
        self._queues: dict[str, asyncio.Queue] = {}
        self._dropped: dict[str, int] = defaultdict(int)

    async def subscribe(self, channel: str, strategy_id: str) -> asyncio.Queue:
        queue = self._queues.setdefault(strategy_id, asyncio.Queue(maxsize=self.queue_maxsize))
        if strategy_id not in self._subscribers[channel] and not self._subscribers[channel]:
            await self._call_pubsub("subscribe", channel)
        self._subscribers[channel].add(strategy_id)
        self._strategy_channels[strategy_id].add(channel)
        return queue

    async def unsubscribe(self, channel: str, strategy_id: str) -> None:
        self._subscribers[channel].discard(strategy_id)
        self._strategy_channels[strategy_id].discard(channel)
        if not self._subscribers[channel]:
            await self._call_pubsub("unsubscribe", channel)
            self._subscribers.pop(channel, None)
        if not self._strategy_channels[strategy_id]:
            self._strategy_channels.pop(strategy_id, None)
            self._queues.pop(strategy_id, None)

    async def handle_message(self, channel: str, data) -> DataEvent:
        payload = json_loads(data) if isinstance(data, str | bytes) else dict(data)
        event = self._event_from_payload(channel, payload)
        if event.kind == "kline" and event.symbol and event.tf:
            self.cache.upsert_candle(event.symbol, event.tf, payload)

        for strategy_id in list(self._subscribers.get(channel, set())):
            queue = self._queues[strategy_id]
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped[strategy_id] += 1
        return event

    async def run(self, stop_event: asyncio.Event, poll_timeout: float = 1.0) -> None:
        while not stop_event.is_set():
            message = await self._get_message(poll_timeout)
            if not message:
                await asyncio.sleep(min(float(poll_timeout), 0.1))
                continue
            if message.get("type") not in {"message", b"message"}:
                continue
            channel = message.get("channel", "")
            data = message.get("data", {})
            if isinstance(channel, bytes):
                channel = channel.decode("utf-8")
            await self.handle_message(str(channel), data)

    async def unsubscribe_strategy(self, strategy_id: str) -> None:
        for channel in list(self._strategy_channels.get(strategy_id, set())):
            await self.unsubscribe(channel, strategy_id)

    def stats(self) -> dict:
        return {
            "active_channels": sorted(self._subscribers),
            "queue_sizes": {sid: q.qsize() for sid, q in self._queues.items()},
            "dropped_events": dict(self._dropped),
        }

    async def _call_pubsub(self, method: str, channel: str) -> None:
        fn = getattr(self.pubsub, method)
        result = fn(channel)
        if asyncio.iscoroutine(result):
            await result

    async def _get_message(self, timeout: float):
        fn = getattr(self.pubsub, "get_message")
        if getattr(self.redis_client, "_runner_inline_redis", False):
            result = fn(ignore_subscribe_messages=True, timeout=timeout)
        else:
            result = await asyncio.to_thread(fn, ignore_subscribe_messages=True, timeout=timeout)
        if asyncio.iscoroutine(result):
            return await result
        return result

    @staticmethod
    def _event_from_payload(channel: str, payload: dict) -> DataEvent:
        if channel.startswith("kline:"):
            return DataEvent(
                channel=channel,
                kind="kline",
                symbol=str(payload.get("symbol", "") or ""),
                tf=str(payload.get("tf", "") or channel.rsplit(":", 1)[-1]),
                payload=payload,
            )
        if channel.startswith("price_alert:"):
            return DataEvent(
                channel=channel,
                kind="price_alert",
                symbol=str(payload.get("symbol", "") or ""),
                payload=payload,
            )
        if channel.startswith("symbols:"):
            return DataEvent(channel=channel, kind="symbols", payload=payload)
        return DataEvent(channel=channel, kind="message", payload=payload)
