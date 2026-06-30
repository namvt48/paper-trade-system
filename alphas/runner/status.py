from __future__ import annotations

import argparse
import json
import os
import sys

import redis as redislib


def show_status(redis_url: str) -> None:
    r = redislib.from_url(redis_url, decode_responses=True)

    alpha_configs = {}
    for key in r.scan_iter("runner:alpha:config:*"):
        raw = r.get(key)
        if raw:
            data = json.loads(raw)
            if data.get("enabled", True):
                alpha_configs[data["alpha_id"]] = data

    runner_groups: dict[str, list[str]] = {}
    unclaimed: list[str] = []
    for alpha_id, cfg in alpha_configs.items():
        lease_key = f"runner:alpha:lease:{alpha_id}"
        owner = r.get(lease_key)
        if owner:
            runner_groups.setdefault(owner, []).append(alpha_id)
        else:
            unclaimed.append(alpha_id)

    for runner_id in sorted(runner_groups):
        alpha_ids = runner_groups[runner_id]
        tf_sets = set()
        for aid in alpha_ids:
            tf_sets.update(alpha_configs[aid].get("tf_set", []))
        print(f"  {runner_id} ({len(alpha_ids)} alphas, TFs: {sorted(tf_sets)})")
        for aid in sorted(alpha_ids):
            tf = ",".join(alpha_configs[aid].get("tf_set", []))
            print(f"    ├── {aid} ({tf})")

    if unclaimed:
        print(f"\n  Unclaimed: {len(unclaimed)} alphas")
        for aid in sorted(unclaimed):
            print(f"    ├── {aid}")

    r.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Show alpha → runner assignment map")
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://paper-redis:6379"))
    args = parser.parse_args()
    print("Runner Assignments:")
    show_status(args.redis_url)


if __name__ == "__main__":
    main()
