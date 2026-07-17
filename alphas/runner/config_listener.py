from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as aioredis

from runner.alpha_claim import claim_alpha_groups
from runner.config_sync import read_alpha_configs_from_redis


logger = logging.getLogger(__name__)


def _derive_async_url(sync_redis_client) -> str:
    """Build a redis:// URL for an async connection from a sync client's
    own connection pool, mirroring ``SharedPubSubManager``'s pattern.
    """
    kwargs = getattr(getattr(sync_redis_client, "connection_pool", None), "connection_kwargs", {})
    host = kwargs.get("host", "localhost")
    port = kwargs.get("port", 6379)
    db = kwargs.get("db", 0)
    return f"redis://{host}:{port}/{db}"


def find_newly_disabled(redis_client, currently_owned: list[str]) -> list[str]:
    disabled = []
    for alpha_id in currently_owned:
        key = f"runner:alpha:config:{alpha_id}"
        raw = redis_client.get(key)
        if raw is None:
            disabled.append(alpha_id)
            continue
        data = json.loads(raw)
        if not data.get("enabled", True):
            disabled.append(alpha_id)
    return disabled


def find_newly_enabled(
    redis_client,
    currently_owned: list[str],
    allowed_alpha_ids: set[str] | None = None,
) -> list[dict]:
    all_configs = read_alpha_configs_from_redis(redis_client)
    unclaimed = []
    for cfg in all_configs:
        if allowed_alpha_ids is not None and cfg["alpha_id"] not in allowed_alpha_ids:
            continue
        if cfg["alpha_id"] not in currently_owned:
            lease_key = f"runner:alpha:lease:{cfg['alpha_id']}"
            if not redis_client.exists(lease_key):
                unclaimed.append(cfg)
    return unclaimed


async def run_config_listener(
    redis_client,
    runner_id: str,
    currently_owned_fn,
    on_disabled=None,
    on_new_alphas=None,
    stop_event: asyncio.Event | None = None,
    max_alphas_per_runner: int = 10,
    ttl_sec: int = 20,
    allowed_alpha_ids: set[str] | None = None,
) -> None:
    # A dedicated async connection for the subscription itself: polling a
    # sync pubsub via run_in_executor(None, ...) would permanently occupy
    # one slot of the runner's shared compute thread pool for the entire
    # process lifetime (this loop never stops polling), competing with
    # alpha scan() work. See 2026-07-16 incident notes in .agents/PLAN.md.
    async_redis = aioredis.from_url(_derive_async_url(redis_client), decode_responses=False)
    pubsub = async_redis.pubsub()
    await pubsub.subscribe("runner:config:updated")
    logger.info("[CONFIG-LISTENER] Subscribed to runner:config:updated")

    try:
        while stop_event is None or not stop_event.is_set():
            loop = asyncio.get_running_loop()
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                msg = None
            if msg and msg["type"] == "message":
                logger.info("[CONFIG-LISTENER] Config update received")
                try:
                    owned = list(currently_owned_fn())
                    disabled = await loop.run_in_executor(
                        None, find_newly_disabled, redis_client, owned,
                    )
                    if disabled and on_disabled:
                        await on_disabled(disabled)

                    new_enabled = await loop.run_in_executor(
                        None, find_newly_enabled, redis_client, owned, allowed_alpha_ids,
                    )
                    if new_enabled and on_new_alphas:
                        already_owned = len(owned)
                        claimed = await loop.run_in_executor(
                            None, claim_alpha_groups,
                            redis_client, runner_id, new_enabled,
                            max_alphas_per_runner, ttl_sec, already_owned,
                        )
                        if claimed:
                            await on_new_alphas(claimed)
                except Exception as exc:
                    logger.warning("[CONFIG-LISTENER] Error processing update: %s", exc)
    finally:
        await pubsub.unsubscribe()
        await pubsub.close()
        await async_redis.close()
        logger.info("[CONFIG-LISTENER] Stopped")
