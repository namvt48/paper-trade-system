from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AlphaConfig:
    alpha_id: str
    strategy: str
    version: str = "1"
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WarmupConfig:
    max_concurrent_mds_requests: int = 3
    max_mds_requests_per_minute: int = 20
    max_symbols_per_mds_request: int = 10
    request_timeout_sec: float = 60.0
    response_cache_ttl_sec: float = 300.0


@dataclass(frozen=True)
class CacheConfig:
    min_retain_bars: int = 0


@dataclass(frozen=True)
class RunnerConfig:
    runner_id: str
    redis_url: str = "redis://localhost:6379"
    mds_redis_url: str = ""
    mds_exchange: str = "binance"
    signal_stream: str = "paper-signals"
    shadow_mode: bool = True
    warmup_min_symbol_coverage: float = 0.90
    data_queue_maxsize: int = 1000
    strategy_event_drop_warn_threshold: int = 100
    lease_ttl_sec: int = 20
    lease_renew_interval_sec: float = 5.0
    warmup: WarmupConfig = field(default_factory=WarmupConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    modules: tuple[str, ...] = ()
    alphas: tuple[AlphaConfig, ...] = ()


def _default_signal_stream(shadow_mode: bool) -> str:
    return "paper-signals" if not shadow_mode else "paper-signals-shadow"


def load_runner_config(path: str | Path, include_disabled: bool = False) -> RunnerConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    runner_raw = dict(raw.get("runner") or {})
    merged = {**raw, **runner_raw}

    shadow_mode = bool(merged.get("shadow_mode", True))
    signal_stream = str(merged.get("signal_stream") or _default_signal_stream(shadow_mode))
    if shadow_mode and signal_stream == "paper-signals":
        raise ValueError("shadow_mode=true must not write to paper-signals")
    runner_id = os.getenv("RUNNER_ID") or str(merged.get("runner_id") or socket.gethostname())
    warmup_raw = dict(merged.get("warmup") or {})
    cache_raw = dict(merged.get("cache") or {})

    alphas = []
    for item in merged.get("alphas", []) or []:
        alpha = AlphaConfig(
            alpha_id=str(item["alpha_id"]),
            strategy=str(item["strategy"]),
            version=str(item.get("version", "1")),
            enabled=bool(item.get("enabled", True)),
            params=dict(item.get("params") or {}),
        )
        if alpha.enabled or include_disabled:
            alphas.append(alpha)

    return RunnerConfig(
        runner_id=runner_id,
        redis_url=str(os.getenv("REDIS_URL") or merged.get("redis_url", "redis://localhost:6379")),
        mds_redis_url=str(os.getenv("MDS_REDIS_URL") or merged.get("mds_redis_url", merged.get("redis_url", "")) or ""),
        mds_exchange=str(os.getenv("MDS_EXCHANGE") or merged.get("mds_exchange", "binance")),
        signal_stream=signal_stream,
        shadow_mode=shadow_mode,
        warmup_min_symbol_coverage=float(merged.get("warmup_min_symbol_coverage", 0.90)),
        data_queue_maxsize=int(merged.get("data_queue_maxsize", 1000)),
        strategy_event_drop_warn_threshold=int(merged.get("strategy_event_drop_warn_threshold", 100)),
        lease_ttl_sec=int(merged.get("lease_ttl_sec", 20)),
        lease_renew_interval_sec=float(merged.get("lease_renew_interval_sec", 5.0)),
        warmup=WarmupConfig(
            max_concurrent_mds_requests=int(warmup_raw.get("max_concurrent_mds_requests", 3)),
            max_mds_requests_per_minute=int(warmup_raw.get("max_mds_requests_per_minute", 20)),
            max_symbols_per_mds_request=int(warmup_raw.get("max_symbols_per_mds_request", 10)),
            request_timeout_sec=float(warmup_raw.get("request_timeout_sec", 60.0)),
            response_cache_ttl_sec=float(warmup_raw.get("response_cache_ttl_sec", 300.0)),
        ),
        cache=CacheConfig(
            min_retain_bars=int(cache_raw.get("min_retain_bars", 0)),
        ),
        modules=tuple(str(m) for m in merged.get("modules", []) or ()),
        alphas=tuple(alphas),
    )
