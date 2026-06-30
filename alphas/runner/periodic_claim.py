from __future__ import annotations

import asyncio
import json
import logging

from runner.alpha_claim import claim_alpha_groups


logger = logging.getLogger(__name__)


def find_unclaimed_alphas(redis_client, allowed_alpha_ids: set[str] | None = None) -> list[dict]:
    configs = []
    for key in redis_client.scan_iter("runner:alpha:config:*"):
        raw = redis_client.get(key)
        if raw is None:
            continue
        data = json.loads(raw)
        if not data.get("enabled", True):
            continue
        if allowed_alpha_ids is not None and data["alpha_id"] not in allowed_alpha_ids:
            continue
        lease_key = f"runner:alpha:lease:{data['alpha_id']}"
        if redis_client.exists(lease_key):
            continue
        configs.append(data)
    return configs


def claim_unclaimed(
    redis_client,
    runner_id: str,
    max_alphas_per_runner: int = 10,
    ttl_sec: int = 20,
    already_owned: int = 0,
    allowed_alpha_ids: set[str] | None = None,
) -> list[dict]:
    unclaimed = find_unclaimed_alphas(redis_client, allowed_alpha_ids)
    if not unclaimed:
        return []
    logger.info("[PERIODIC-CLAIM] Found %d unclaimed alphas", len(unclaimed))
    return claim_alpha_groups(redis_client, runner_id, unclaimed, max_alphas_per_runner, ttl_sec, already_owned=already_owned)


async def run_periodic_claim(
    redis_client,
    runner_id: str,
    max_alphas_per_runner: int,
    ttl_sec: int,
    interval_sec: float,
    stop_event: asyncio.Event,
    on_new_alphas=None,
    retry_delay_sec: float = 5.0,
    currently_owned_fn=None,
    allowed_alpha_ids: set[str] | None = None,
) -> None:
    logger.info("[PERIODIC-CLAIM] Starting interval=%.0fs", interval_sec)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            break
        except asyncio.TimeoutError:
            pass

        try:
            already_owned = len(currently_owned_fn()) if currently_owned_fn else 0
            loop = asyncio.get_running_loop()
            new = await loop.run_in_executor(
                None, claim_unclaimed, redis_client, runner_id, max_alphas_per_runner, ttl_sec, already_owned,
                allowed_alpha_ids,
            )
            if new and on_new_alphas:
                await on_new_alphas(new)
        except Exception as exc:
            logger.warning("[PERIODIC-CLAIM] Error (retry in %.0fs): %s", retry_delay_sec, exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_delay_sec)
            except asyncio.TimeoutError:
                pass

    logger.info("[PERIODIC-CLAIM] Stopped")
