#!/usr/bin/env python3
from __future__ import annotations

import json
import os

import redis


def main() -> int:
    paper = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6382"), decode_responses=True)
    keys = sorted(paper.scan_iter("paper:positions:snapshot:*"))
    failures = []
    snapshots = []
    for key in keys:
        payload = json.loads(paper.get(key))
        alpha_id = payload["alpha_id"]
        heartbeat = paper.get(f"paper:alpha-runtime:{alpha_id}")
        snapshots.append({
            "alpha_id": alpha_id,
            "revision": payload.get("revision"),
            "authoritative_positions": len(payload.get("positions", [])),
            "heartbeat_present": bool(heartbeat),
        })
        if payload.get("positions") and not heartbeat:
            failures.append(f"{alpha_id}: positions exist without runtime heartbeat")
    print(json.dumps({"healthy": not failures, "snapshots": snapshots, "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
