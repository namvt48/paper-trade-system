from __future__ import annotations

import logging
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)


@dataclass
class AlphaGroup:
    tf_key: str
    tf_set: list[str]
    alpha_ids: list[str]
    configs: list[dict]


def group_alphas_by_tf_set(
    configs: list[dict],
    max_alphas_per_runner: int = 10,
) -> list[AlphaGroup]:
    by_tf: dict[str, list[dict]] = {}
    for cfg in configs:
        tf_set = tuple(sorted(cfg.get("tf_set", ["1m"])))
        tf_key = ",".join(tf_set)
        by_tf.setdefault(tf_key, []).append(cfg)

    groups: list[AlphaGroup] = []
    for tf_key, cfgs in by_tf.items():
        tf_set = list(sorted(cfgs[0].get("tf_set", ["1m"])))
        for i in range(0, len(cfgs), max_alphas_per_runner):
            chunk = cfgs[i : i + max_alphas_per_runner]
            groups.append(AlphaGroup(
                tf_key=tf_key,
                tf_set=tf_set,
                alpha_ids=[c["alpha_id"] for c in chunk],
                configs=chunk,
            ))

    groups.sort(key=lambda g: len(g.alpha_ids), reverse=True)
    return groups


_COMPARE_AND_DELETE_LUA = """
local val = redis.call('GET', KEYS[1])
if val == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return 1
end
return 0
"""


def _release_lease(redis_client, lease_key: str, runner_id: str) -> None:
    try:
        redis_client.eval(_COMPARE_AND_DELETE_LUA, 1, lease_key, runner_id)
    except Exception:
        existing = redis_client.get(lease_key)
        if isinstance(existing, bytes):
            existing = existing.decode()
        if existing == runner_id:
            redis_client.delete(lease_key)


def _decode_owner(owner) -> str | None:
    if isinstance(owner, bytes):
        return owner.decode()
    return owner


def claim_alpha_group(
    redis_client,
    runner_id: str,
    group: AlphaGroup,
    ttl_sec: int = 20,
) -> bool:
    acquired: list[str] = []
    for alpha_id in group.alpha_ids:
        key = f"runner:alpha:lease:{alpha_id}"
        ok = redis_client.set(key, runner_id, nx=True, ex=ttl_sec)
        if not ok:
            existing_owner = _decode_owner(redis_client.get(key))
            if existing_owner == runner_id:
                redis_client.expire(key, ttl_sec)
                acquired.append(alpha_id)
                continue
            for aid in acquired:
                lease_key = f"runner:alpha:lease:{aid}"
                _release_lease(redis_client, lease_key, runner_id)
            logger.info(
                "[CLAIM] Group %s partial fail at alpha=%s, rolled back %d",
                group.tf_key, alpha_id, len(acquired),
            )
            return False
        acquired.append(alpha_id)

    logger.info(
        "[CLAIM] Group %s claimed %d alphas: %s",
        group.tf_key, len(acquired), ",".join(acquired),
    )
    return True


def claim_alpha_groups(
    redis_client,
    runner_id: str,
    configs: list[dict],
    max_alphas_per_runner: int = 10,
    ttl_sec: int = 20,
    already_owned: int = 0,
) -> list[dict]:
    remaining = max(0, max_alphas_per_runner - already_owned)
    if remaining <= 0:
        logger.info("[CLAIM] Runner %s already at capacity (%d), skipping", runner_id, already_owned)
        return []
    groups = group_alphas_by_tf_set(configs, max_alphas_per_runner)
    claimed_configs: list[dict] = []

    for group in groups:
        slots = remaining - len(claimed_configs)
        if slots <= 0:
            break
        claim_group = group
        if len(group.configs) > slots:
            claim_group = AlphaGroup(
                tf_key=group.tf_key,
                tf_set=group.tf_set,
                alpha_ids=group.alpha_ids[:slots],
                configs=group.configs[:slots],
            )
        if claim_alpha_group(redis_client, runner_id, claim_group, ttl_sec):
            claimed_configs.extend(claim_group.configs)

    logger.info(
        "[CLAIM] Runner %s claimed %d/%d alphas across %d groups",
        runner_id, len(claimed_configs), len(configs), len(groups),
    )
    return claimed_configs
