from __future__ import annotations

import argparse
import os
import subprocess
import time
from dataclasses import dataclass

import redis


@dataclass(frozen=True)
class RunnerLoad:
    cpu_pct: float
    memory_pct: float


@dataclass
class ScaleState:
    high_since: float | None = None
    low_since: float | None = None
    last_action_at: float = 0.0


def decide_scale(
    loads: list[RunnerLoad],
    replicas: int,
    state: ScaleState,
    *,
    now: float,
    min_replicas: int = 1,
    max_replicas: int = 5,
    sustain_sec: float = 120.0,
    cooldown_sec: float = 300.0,
) -> int:
    if now - state.last_action_at < cooldown_sec:
        return replicas

    high = any(load.cpu_pct > 75.0 or load.memory_pct > 80.0 for load in loads)
    low = bool(loads) and all(load.cpu_pct < 30.0 and load.memory_pct < 50.0 for load in loads)

    if high:
        if state.high_since is None:
            state.high_since = now
        state.low_since = None
        if now - state.high_since >= sustain_sec and replicas < max_replicas:
            state.last_action_at = now
            state.high_since = None
            return replicas + 1
    elif low:
        if state.low_since is None:
            state.low_since = now
        state.high_since = None
        if now - state.low_since >= sustain_sec and replicas > min_replicas:
            state.last_action_at = now
            state.low_since = None
            return replicas - 1
    else:
        state.high_since = None
        state.low_since = None
    return replicas


def acquire_lock(redis_client, owner: str, ttl_sec: int = 30) -> bool:
    return bool(redis_client.set("runner:scaler:lock", owner, nx=True, ex=ttl_sec))


def parse_percent(value: str) -> float:
    return float(str(value).strip().rstrip("%") or 0.0)


def runner_container_ids() -> list[str]:
    result = subprocess.run(
        ["docker", "compose", "--profile", "runner", "ps", "-q", "alpha-runner"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def read_runner_loads(container_ids: list[str]) -> list[RunnerLoad]:
    if not container_ids:
        return []
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemPerc}}", *container_ids],
        check=True,
        capture_output=True,
        text=True,
    )
    loads = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            loads.append(RunnerLoad(parse_percent(parts[0]), parse_percent(parts[1])))
    return loads


def run_compose_scale(replicas: int) -> None:
    subprocess.run(
        ["docker", "compose", "--profile", "runner", "up", "-d", "--scale", f"alpha-runner={replicas}", "alpha-runner"],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replicas", type=int)
    parser.add_argument("--max-replicas", type=int, default=int(os.getenv("RUNNER_MAX_REPLICAS", "5")))
    parser.add_argument("--min-replicas", type=int, default=int(os.getenv("RUNNER_MIN_REPLICAS", "1")))
    parser.add_argument("--interval-sec", type=float, default=float(os.getenv("RUNNER_SCALER_INTERVAL_SEC", "30")))
    parser.add_argument("--sustain-sec", type=float, default=float(os.getenv("RUNNER_SCALER_SUSTAIN_SEC", "120")))
    parser.add_argument("--cooldown-sec", type=float, default=float(os.getenv("RUNNER_SCALER_COOLDOWN_SEC", "300")))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://paper-redis:6379"))
    args = parser.parse_args()

    client = redis.from_url(args.redis_url, decode_responses=True)
    owner = f"{os.uname().nodename}:{os.getpid()}"
    if not acquire_lock(client, owner):
        print("runner scaler lock is held; exiting")
        return

    if args.replicas is not None:
        run_compose_scale(max(args.min_replicas, min(args.replicas, args.max_replicas)))
        return

    state = ScaleState()
    while True:
        containers = runner_container_ids()
        current = max(args.min_replicas, len(containers))
        loads = read_runner_loads(containers)
        desired = decide_scale(
            loads,
            current,
            state,
            now=time.time(),
            min_replicas=args.min_replicas,
            max_replicas=args.max_replicas,
            sustain_sec=args.sustain_sec,
            cooldown_sec=args.cooldown_sec,
        )
        print(f"runner-scaler current={current} desired={desired} loads={loads}")
        if desired != current:
            run_compose_scale(desired)
        if args.once:
            return
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
