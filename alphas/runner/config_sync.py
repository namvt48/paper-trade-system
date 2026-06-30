from __future__ import annotations

import argparse
import json
import logging
import re
from typing import Any

import redis

from runner.config import AlphaConfig, load_runner_config
from runner.strategy.registry import StrategyRegistry

ALPHA_CONFIG_PREFIX = "runner:alpha:config:"
CONFIG_UPDATED_CHANNEL = "runner:config:updated"

logger = logging.getLogger(__name__)


def _build_registry(cfg) -> StrategyRegistry:
    from runner.main import build_registry

    return build_registry(cfg.modules)


def _derive_tf_set(channels: list[str]) -> list[str]:
    tfs: list[str] = []
    for ch in channels:
        m = re.search(r"(\d+m|\d+h|\d+d)", ch)
        if m:
            tfs.append(m.group(1))
    return sorted(set(tfs))


def sync_config_to_redis(config_path: str, redis_client, *, prune: bool = True) -> int:
    cfg = load_runner_config(config_path, include_disabled=True)
    registry = _build_registry(cfg)

    written = 0
    active_keys: set[str] = set()

    for alpha in cfg.alphas:
        key = f"{ALPHA_CONFIG_PREFIX}{alpha.alpha_id}"
        if not alpha.enabled:
            try:
                redis_client.delete(key)
            except Exception as exc:
                logger.warning("[CONFIG-SYNC] Failed to delete disabled alpha %s: %s", alpha.alpha_id, exc)
            continue

        try:
            cls = registry.get_class(alpha.strategy)
        except (KeyError, Exception) as exc:
            logger.warning("[CONFIG-SYNC] Unknown strategy %s for alpha %s: %s", alpha.strategy, alpha.alpha_id, exc)
            continue

        try:
            channels = cls.get_required_channels(alpha.params)
        except Exception as exc:
            logger.warning("[CONFIG-SYNC] Failed to derive channels for alpha %s: %s", alpha.alpha_id, exc)
            continue
        tf_set = _derive_tf_set(channels)

        blob: dict[str, Any] = {
            "alpha_id": alpha.alpha_id,
            "strategy": alpha.strategy,
            "version": alpha.version,
            "enabled": alpha.enabled,
            "params": alpha.params,
            "required_channels": channels,
            "tf_set": tf_set,
        }
        redis_client.set(key, json.dumps(blob))
        active_keys.add(key)
        written += 1

    if prune:
        for existing in redis_client.scan_iter(match=f"{ALPHA_CONFIG_PREFIX}*"):
            existing_str = existing.decode() if isinstance(existing, bytes) else existing
            if existing_str not in active_keys:
                redis_client.delete(existing_str)

    redis_client.publish(CONFIG_UPDATED_CHANNEL, json.dumps({"runner_id": cfg.runner_id}))
    return written


def read_alpha_configs_from_redis(redis_client) -> list[dict]:
    configs: list[dict] = []
    try:
        keys = list(redis_client.scan_iter(match=f"{ALPHA_CONFIG_PREFIX}*"))
    except Exception as exc:
        logger.warning("[CONFIG-SYNC] Redis scan failed: %s", exc)
        return configs

    for key in keys:
        key_str = key.decode() if isinstance(key, bytes) else key
        try:
            raw = redis_client.get(key_str)
            if raw is None:
                continue
            blob = json.loads(raw)
            if blob.get("enabled", True):
                configs.append(blob)
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("[CONFIG-SYNC] Skipping corrupt config key %s: %s", key_str, exc)
    return configs


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync runner-config.yaml to Redis")
    parser.add_argument("--config", required=True, help="Path to runner-config.yaml")
    parser.add_argument("--redis-url", default="redis://localhost:6379", help="Redis URL")
    parser.add_argument("--no-prune", action="store_true", help="Do not delete alpha configs owned by other runner config files")
    args = parser.parse_args()

    r = redis.from_url(args.redis_url, decode_responses=True)
    count = sync_config_to_redis(args.config, r, prune=not args.no_prune)
    print(f"synced {count} alpha configs to redis")


if __name__ == "__main__":
    main()
