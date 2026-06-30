from __future__ import annotations

import asyncio
import json
import logging

from runner.alpha_claim import claim_alpha_groups
from runner.config_sync import read_alpha_configs_from_redis


logger = logging.getLogger(__name__)


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
    pubsub = redis_client.pubsub()
    pubsub.subscribe("runner:config:updated")
    logger.info("[CONFIG-LISTENER] Subscribed to runner:config:updated")

    try:
        while stop_event is None or not stop_event.is_set():
            loop = asyncio.get_running_loop()
            msg = await loop.run_in_executor(None, pubsub.get_message, 1.0)
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
        pubsub.unsubscribe()
        if hasattr(pubsub, 'close'):
            pubsub.close()
        logger.info("[CONFIG-LISTENER] Stopped")
