from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import redis.asyncio as aioredis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)

from base.json_utils import loads as json_loads
from runner.data_layer.cache import SharedCandleCache

logger = logging.getLogger(__name__)

_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def _tf_ms(tf: str) -> int:
    return _TF_MINUTES.get(tf, 1) * 60 * 1000


@dataclass(frozen=True)
class DataEvent:
    channel: str
    kind: str
    symbol: str = ""
    tf: str = ""
    payload: dict | None = None
    received_monotonic: float = 0.0


class SharedPubSubManager:
    def __init__(
        self, redis_client, cache: SharedCandleCache, queue_maxsize: int = 1000
    ):
        self._sync_redis = redis_client
        self._async_redis: aioredis.Redis | None = None
        self.pubsub: aioredis.client.PubSub | None = None
        self._inline = getattr(redis_client, "_runner_inline_redis", False)
        if self._inline:
            self.pubsub = redis_client.pubsub()
        self.cache = cache
        self.queue_maxsize = int(queue_maxsize)
        self._subscribers: dict[str, set[str]] = defaultdict(set)
        self._strategy_channels: dict[str, set[str]] = defaultdict(set)
        self._queues: dict[str, asyncio.Queue] = {}
        self._dropped: dict[str, int] = defaultdict(int)
        self._warmup_manager = None
        self._reconnect_staleness_candles: int = 5
        self._trading_session = None
        self._last_message_time: float = time.monotonic()
        self._stale_check_interval: float = 30.0
        self._metrics = None
        self._redis_url: str | None = None
        if (
            not self._inline
            and hasattr(redis_client, "connection_pool")
            and hasattr(redis_client.connection_pool, "connection_kwargs")
        ):
            kwargs = redis_client.connection_pool.connection_kwargs
            host = kwargs.get("host", "localhost")
            port = kwargs.get("port", 6379)
            db = kwargs.get("db", 0)
            self._redis_url = f"redis://{host}:{port}/{db}"

    async def _ensure_async_pubsub(self) -> aioredis.client.PubSub:
        if self.pubsub is not None:
            return self.pubsub
        if self._redis_url is None:
            self._redis_url = "redis://localhost:6379/0"
        self._async_redis = aioredis.from_url(self._redis_url, decode_responses=False)
        self.pubsub = self._async_redis.pubsub()
        return self.pubsub

    def set_reconnect_handler(
        self,
        warmup_manager,
        staleness_candles: int = 5,
        trading_session=None,
    ) -> None:
        self._warmup_manager = warmup_manager
        self._reconnect_staleness_candles = staleness_candles
        self._trading_session = trading_session

    def _outside_trading_session(self) -> bool:
        session = getattr(self, "_trading_session", None)
        if session is None:
            return False
        try:
            return not session.is_active()
        except Exception:
            return False

    async def subscribe(self, channel: str, strategy_id: str) -> asyncio.Queue:
        queue = self._queues.setdefault(
            strategy_id, asyncio.Queue(maxsize=self.queue_maxsize)
        )
        if (
            strategy_id not in self._subscribers[channel]
            and not self._subscribers[channel]
        ):
            if self._inline:
                self.pubsub.subscribe(channel)
            else:
                ps = await self._ensure_async_pubsub()
                await ps.subscribe(channel)
            logger.info(
                "[PUBSUB] Subscribed to channel=%s for strategy=%s",
                channel,
                strategy_id,
            )
        self._subscribers[channel].add(strategy_id)
        self._strategy_channels[strategy_id].add(channel)
        return queue

    async def unsubscribe(self, channel: str, strategy_id: str) -> None:
        self._subscribers[channel].discard(strategy_id)
        self._strategy_channels[strategy_id].discard(channel)
        if not self._subscribers[channel]:
            if self.pubsub is not None:
                if self._inline:
                    self.pubsub.unsubscribe(channel)
                else:
                    await self.pubsub.unsubscribe(channel)
            self._subscribers.pop(channel, None)
        if not self._strategy_channels[strategy_id]:
            self._strategy_channels.pop(strategy_id, None)
            self._queues.pop(strategy_id, None)

    async def handle_message(self, channel: str, data) -> DataEvent:
        payload = json_loads(data) if isinstance(data, str | bytes) else dict(data)
        event = self._event_from_payload(channel, payload)
        event = DataEvent(
            channel=event.channel,
            kind=event.kind,
            symbol=event.symbol,
            tf=event.tf,
            payload=event.payload,
            received_monotonic=time.perf_counter(),
        )
        if event.kind == "kline" and event.symbol and event.tf:
            self.cache.upsert_candle(event.symbol, event.tf, payload)

        for strategy_id in list(self._subscribers.get(channel, set())):
            queue = self._queues[strategy_id]
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped[strategy_id] += 1
                if (
                    self._dropped[strategy_id] <= 3
                    or self._dropped[strategy_id] % 100 == 0
                ):
                    logger.warning(
                        "[PUBSUB] Queue full for strategy=%s, dropped total=%d",
                        strategy_id,
                        self._dropped[strategy_id],
                    )
        return event

    async def run(self, stop_event: asyncio.Event, poll_timeout: float = 1.0) -> None:
        self._last_message_time = time.monotonic()
        last_stale_check = time.monotonic()
        last_liveness_log = time.monotonic()
        logger.info(
            "[PUBSUB] Run loop started, subscribed channels: %s",
            list(self._subscribers.keys()),
        )

        while not stop_event.is_set():
            try:
                message = await self._get_message(poll_timeout)
            except (
                RedisConnectionError,
                RedisTimeoutError,
                OSError,
                ConnectionResetError,
            ) as exc:
                logger.warning("[PUBSUB] Connection error, retrying in 2s: %s", exc)
                if self._metrics:
                    self._metrics.inc("pubsub_connection_error_total")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=2.0)
                    break
                except asyncio.TimeoutError:
                    continue
            except Exception as exc:
                logger.exception("[PUBSUB] Unexpected error getting message: %s", exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    continue

            now = time.monotonic()
            if now - last_liveness_log >= 60.0:
                last_liveness_log = now
                logger.info(
                    "[PUBSUB] Run loop alive, channels=%d last_msg_ago=%.0fs",
                    len(self._subscribers),
                    now - self._last_message_time,
                )

            if not message:
                now = time.monotonic()
                if (
                    now - last_stale_check >= self._stale_check_interval
                    and self._warmup_manager
                ):
                    last_stale_check = now
                    if self._metrics:
                        self._metrics.inc("stale_check_total")
                    if self._outside_trading_session():
                        continue
                    try:
                        if self._is_data_stale(
                            self._warmup_manager, self._reconnect_staleness_candles
                        ):
                            logger.warning(
                                "[PUBSUB] Data stale during periodic check — triggering reconnect warmup"
                            )
                            await self._on_reconnect()
                    except Exception as exc:
                        logger.exception(
                            "[PUBSUB] Stale check/reconnect error: %s", exc
                        )
                await asyncio.sleep(min(float(poll_timeout), 0.1))
                continue

            msg_type = message.get("type", "")
            if msg_type not in {"message", b"message"}:
                continue

            self._last_message_time = time.monotonic()
            channel = message.get("channel", "")
            data = message.get("data", {})
            if isinstance(channel, bytes):
                channel = channel.decode("utf-8")
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            logger.debug(
                "[PUBSUB] Received message on channel=%s data_len=%d",
                channel,
                len(str(data)),
            )
            try:
                await self.handle_message(str(channel), data)
            except Exception as exc:
                logger.exception(
                    "[PUBSUB] Error handling message on %s: %s", channel, exc
                )

    async def unsubscribe_strategy(self, strategy_id: str) -> None:
        for channel in list(self._strategy_channels.get(strategy_id, set())):
            await self.unsubscribe(channel, strategy_id)

    def stats(self) -> dict:
        return {
            "active_channels": sorted(self._subscribers),
            "queue_sizes": {sid: q.qsize() for sid, q in self._queues.items()},
            "dropped_events": dict(self._dropped),
        }

    def _is_data_stale(
        self,
        warmup_manager,
        staleness_candles: int = 5,
    ) -> bool:
        silence_sec = time.monotonic() - self._last_message_time
        required_tfs = warmup_manager.get_required_tfs()
        if required_tfs:
            min_tf_ms = min(_tf_ms(tf) for tf in required_tfs)
            silence_threshold_sec = max(60.0, (min_tf_ms / 1000.0) * staleness_candles)
        else:
            silence_threshold_sec = 60.0
        if silence_sec > silence_threshold_sec:
            return True

        for tf in required_tfs:
            max_ts = self.cache.get_max_timestamp(tf)
            if max_ts is None:
                max_ts = 0
            now_ms = int(time.time() * 1000)
            gap_ms = now_ms - max_ts
            tf_ms_val = _tf_ms(tf)
            if gap_ms > tf_ms_val * staleness_candles:
                return True
        return False

    def _find_stale_symbols(
        self, warmup_manager, staleness_candles: int
    ) -> list[tuple[str, str]]:
        stale = []
        now_ms = int(time.time() * 1000)
        excluded = getattr(warmup_manager, "excluded_symbols", set())
        for tf in warmup_manager.get_required_tfs():
            for symbol in self.cache.get_symbols_with_data(tf):
                if symbol in excluded:
                    continue
                ts = self.cache.get_latest_timestamp(symbol, tf)
                if ts is None:
                    stale.append((symbol, tf))
                    continue
                gap_ms = now_ms - ts
                tf_ms_val = _tf_ms(tf)
                if gap_ms > tf_ms_val * staleness_candles:
                    stale.append((symbol, tf))
        return stale

    async def _on_reconnect(
        self, warmup_manager=None, staleness_candles: int | None = None
    ) -> None:
        if self._outside_trading_session():
            logger.info("[PUBSUB] Skipping reconnect warmup — outside trading session")
            return
        wm = warmup_manager or self._warmup_manager
        sc = (
            staleness_candles
            if staleness_candles is not None
            else self._reconnect_staleness_candles
        )
        if wm is None:
            logger.warning(
                "[PUBSUB] Reconnected but no warmup_manager set — skipping reconnect warmup"
            )
            return

        logger.warning("[PUBSUB] Data stale — checking per-symbol freshness")

        stale_symbols = self._find_stale_symbols(wm, sc)
        if not stale_symbols:
            logger.info("[PUBSUB] All data still fresh — resuming")
            return

        if self._metrics:
            self._metrics.inc("stale_detected_total")

        logger.warning(
            "[PUBSUB] %d symbols have stale data — re-reading from snapshots",
            len(stale_symbols),
        )

        if wm.snapshot_reader:
            for symbol, tf in stale_symbols:
                bars = wm._requirements.get((symbol, tf), 0)
                candles = wm.snapshot_reader.load(symbol, tf, bars)
                if candles:
                    if self._metrics:
                        self._metrics.inc("reconnect_snapshot_hit_total")
                    for candle in candles:
                        self.cache.upsert_candle(symbol, tf, candle)

        if self._is_data_stale(wm, sc):
            logger.warning(
                "[PUBSUB] Still stale after snapshot re-read — triggering full warmup"
            )
            if self._metrics:
                self._metrics.inc("reconnect_full_warmup_total")
            await wm.run_synced_warmup()
        else:
            logger.info("[PUBSUB] Data refreshed from snapshots — resuming")

    async def _call_pubsub(self, method: str, channel: str) -> None:
        if self.pubsub is None:
            return
        fn = getattr(self.pubsub, method)
        result = fn(channel)
        if asyncio.iscoroutine(result):
            await result

    async def _get_message(self, timeout: float):
        if self._inline:
            return self.pubsub.get_message(
                ignore_subscribe_messages=True, timeout=timeout
            )
        ps = await self._ensure_async_pubsub()
        try:
            result = await asyncio.wait_for(
                ps.get_message(ignore_subscribe_messages=True, timeout=0.05),
                timeout=min(timeout + 2.0, 5.0),
            )
        except asyncio.TimeoutError:
            return None
        except (RedisConnectionError, RedisTimeoutError, OSError, ConnectionResetError):
            logger.warning("[PUBSUB] Connection error in get_message — resetting")
            await self._reset_pubsub()
            return None
        except Exception as exc:
            logger.warning(
                "[PUBSUB] Unexpected error in get_message: %s — resetting", exc
            )
            await self._reset_pubsub()
            return None
        return result

    async def _reset_pubsub(self) -> None:
        if self._inline:
            return
        if self.pubsub is not None:
            try:
                await self.pubsub.unsubscribe()
                await self.pubsub.close()
            except Exception:
                pass
        try:
            self._async_redis = aioredis.from_url(
                self._redis_url or "redis://localhost:6379/0", decode_responses=False
            )
            self.pubsub = self._async_redis.pubsub()
            for channel in self._subscribers:
                await self.pubsub.subscribe(channel)
        except Exception as exc:
            logger.warning("[PUBSUB] Failed to reset pubsub: %s", exc)

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
