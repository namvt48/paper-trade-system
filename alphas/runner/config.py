from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from datetime import datetime
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
    max_concurrent_mds_requests: int = 6
    max_mds_requests_per_minute: int = 60
    max_symbols_per_mds_request: int = 30
    request_timeout_sec: float = 60.0
    response_cache_ttl_sec: float = 300.0
    mds_ready_timeout_sec: int = 900
    min_warmup_coverage_pct: float = 0.60
    sync_tolerance_candles: int = 1
    reconnect_staleness_candles: int = 5
    parquet_max_staleness_sec: float = 21600


@dataclass(frozen=True)
class CacheConfig:
    min_retain_bars: int = 0


@dataclass(frozen=True)
class TradingSession:
    """Optional daily trading window for the runner.

    When set, the pubsub stale-check skips reconnect warmup while the market is
    closed (e.g. VN30 futures 08:45-14:25 Asia/Ho_Chi_Minh), so a closed market
    does not trigger endless warmup requests for data that will not arrive.
    ``None`` start/end means 24/7 (binance default).
    """

    start: str = "00:00"
    end: str = "23:59"
    timezone: str = "UTC"
    trade_weekends: bool = True

    def is_active(self, now: datetime | None = None) -> bool:
        from zoneinfo import ZoneInfo

        if now is None:
            now = datetime.now(ZoneInfo(self.timezone))
        else:
            now = now.astimezone(ZoneInfo(self.timezone))
        if not self.trade_weekends and now.weekday() >= 5:
            return False
        start_min = _clock_to_minutes(self.start)
        end_min = _clock_to_minutes(self.end)
        now_min = now.hour * 60 + now.minute
        if start_min <= end_min:
            return start_min <= now_min < end_min
        return now_min >= start_min or now_min < end_min


def _clock_to_minutes(value: str) -> int:
    try:
        parts = str(value).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0


@dataclass(frozen=True)
class RunnerConfig:
    runner_id: str
    redis_url: str = "redis://localhost:6379"
    mds_redis_url: str = ""
    mds_exchange: str = "binance"
    mds_redis_socket_timeout_sec: float = 10.0
    signal_stream: str = "paper-signals"
    shadow_mode: bool = True
    warmup_min_symbol_coverage: float = 0.90
    data_queue_maxsize: int = 1000
    strategy_event_drop_warn_threshold: int = 100
    compute_workers: int = 4
    lease_ttl_sec: int = 20
    lease_renew_interval_sec: float = 5.0
    max_alphas_per_runner: int = 10
    claim_interval_sec: int = 30
    runner_cache_dir: str = ""
    claim_retry_delay_sec: int = 5
    warmup: WarmupConfig = field(default_factory=WarmupConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    trading_session: TradingSession | None = None
    modules: tuple[str, ...] = ()
    alphas: tuple[AlphaConfig, ...] = ()


def _default_signal_stream(shadow_mode: bool) -> str:
    return "paper-signals" if not shadow_mode else "paper-signals-shadow"


def load_runner_config(
    path: str | Path, include_disabled: bool = False
) -> RunnerConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    runner_raw = dict(raw.get("runner") or {})
    merged = {**raw, **runner_raw}

    shadow_mode = bool(merged.get("shadow_mode", True))
    signal_stream = str(
        merged.get("signal_stream") or _default_signal_stream(shadow_mode)
    )
    if shadow_mode and signal_stream == "paper-signals":
        raise ValueError("shadow_mode=true must not write to paper-signals")
    runner_id = os.getenv("RUNNER_ID") or str(
        merged.get("runner_id") or socket.gethostname()
    )
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
        redis_url=str(
            os.getenv("REDIS_URL") or merged.get("redis_url", "redis://localhost:6379")
        ),
        mds_redis_url=str(
            os.getenv("MDS_REDIS_URL")
            or merged.get("mds_redis_url", merged.get("redis_url", ""))
            or ""
        ),
        mds_exchange=str(
            os.getenv("MDS_EXCHANGE") or merged.get("mds_exchange", "binance")
        ),
        mds_redis_socket_timeout_sec=float(
            os.getenv("MDS_REDIS_SOCKET_TIMEOUT_SEC")
            or merged.get("mds_redis_socket_timeout_sec", 10.0)
        ),
        signal_stream=signal_stream,
        shadow_mode=shadow_mode,
        warmup_min_symbol_coverage=float(
            merged.get("warmup_min_symbol_coverage", 0.90)
        ),
        data_queue_maxsize=int(merged.get("data_queue_maxsize", 1000)),
        strategy_event_drop_warn_threshold=int(
            merged.get("strategy_event_drop_warn_threshold", 100)
        ),
        compute_workers=int(
            os.getenv("RUNNER_COMPUTE_WORKERS") or merged.get("compute_workers", 4)
        ),
        lease_ttl_sec=int(merged.get("lease_ttl_sec", 20)),
        lease_renew_interval_sec=float(merged.get("lease_renew_interval_sec", 5.0)),
        max_alphas_per_runner=int(
            os.getenv("MAX_ALPHAS_PER_RUNNER")
            or merged.get("max_alphas_per_runner", 10)
        ),
        claim_interval_sec=int(
            os.getenv("CLAIM_INTERVAL_SEC") or merged.get("claim_interval_sec", 30)
        ),
        runner_cache_dir=str(
            os.getenv("RUNNER_CACHE_DIR") or merged.get("runner_cache_dir", "")
        ),
        claim_retry_delay_sec=int(
            os.getenv("CLAIM_RETRY_DELAY_SEC") or merged.get("claim_retry_delay_sec", 5)
        ),
        warmup=WarmupConfig(
            max_concurrent_mds_requests=int(
                warmup_raw.get("max_concurrent_mds_requests", 6)
            ),
            max_mds_requests_per_minute=int(
                warmup_raw.get("max_mds_requests_per_minute", 60)
            ),
            max_symbols_per_mds_request=int(
                warmup_raw.get("max_symbols_per_mds_request", 30)
            ),
            request_timeout_sec=float(warmup_raw.get("request_timeout_sec", 60.0)),
            response_cache_ttl_sec=float(
                warmup_raw.get("response_cache_ttl_sec", 300.0)
            ),
            mds_ready_timeout_sec=int(warmup_raw.get("mds_ready_timeout_sec", 900)),
            min_warmup_coverage_pct=float(
                warmup_raw.get("min_warmup_coverage_pct", 0.60)
            ),
            sync_tolerance_candles=int(warmup_raw.get("sync_tolerance_candles", 1)),
            reconnect_staleness_candles=int(
                warmup_raw.get("reconnect_staleness_candles", 5)
            ),
            parquet_max_staleness_sec=float(
                warmup_raw.get("parquet_max_staleness_sec", 7200)
            ),
        ),
        cache=CacheConfig(
            min_retain_bars=int(cache_raw.get("min_retain_bars", 0)),
        ),
        trading_session=_parse_trading_session(merged.get("trading_session")),
        modules=tuple(str(m) for m in merged.get("modules", []) or ()),
        alphas=tuple(alphas),
    )


def _parse_trading_session(value: Any) -> TradingSession | None:
    if value is None:
        return None
    if isinstance(value, str):
        return TradingSession(timezone=value)
    if not isinstance(value, dict):
        return None
    return TradingSession(
        start=str(value.get("start", "00:00")),
        end=str(value.get("end", "23:59")),
        timezone=str(value.get("timezone", "UTC")),
        trade_weekends=bool(value.get("trade_weekends", True)),
    )
