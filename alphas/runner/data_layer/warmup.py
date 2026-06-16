from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Mapping

from redis.exceptions import TimeoutError as RedisTimeoutError

from runner.data_layer.cache import SharedCandleCache
from runner.metrics import RunnerMetrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True, order=True)
class WarmupRequirement:
    symbol: str
    tf: str
    bars: int


def bars_bucket(bars: int) -> str:
    bars = int(bars)
    if bars <= 500:
        return "le_500"
    if bars <= 2000:
        return "le_2000"
    return "gt_2000"


def request_key(exchange: str, tf: str, bars: int, symbols: Iterable[str]) -> str:
    stable_symbols = sorted(set(str(symbol) for symbol in symbols))
    digest = hashlib.sha1(",".join(stable_symbols).encode("utf-8")).hexdigest()
    return f"{exchange}|{tf}|{int(bars)}|{digest}"


RequestResult = set[tuple[str, str]] | dict[tuple[str, str], list[dict]] | dict[str, list[dict]]
RequestBackend = Callable[[tuple[WarmupRequirement, ...]], Awaitable[RequestResult]]


class MDSWarmupBackend:
    handles_timeout = True

    def __init__(
        self,
        redis_client,
        exchange: str,
        runner_id: str,
        timeout_sec: float = 60.0,
    ) -> None:
        self.redis = redis_client
        self.exchange = exchange
        self.runner_id = runner_id
        self.timeout_sec = float(timeout_sec)

    async def __call__(self, requirements: tuple[WarmupRequirement, ...]) -> dict[tuple[str, str], list[dict]]:
        if not requirements:
            return {}
        stream, fields, response_stream, symbols = self.build_request(requirements)
        await self._redis_call(self.redis.xadd, stream, fields)
        tf = fields["tf"]
        return await self._read_response(response_stream, symbols, tf)

    def build_request(
        self,
        requirements: tuple[WarmupRequirement, ...],
    ) -> tuple[str, dict[str, str], str, list[str]]:
        tf = requirements[0].tf
        bars = max(req.bars for req in requirements)
        symbols = sorted({req.symbol for req in requirements})
        key = request_key(self.exchange, tf, bars, symbols)
        req_id = f"{self.runner_id}:warmup:{key.split('|')[-1]}"
        response_stream = f"warmup:response:{req_id}"
        stream = f"warmup:request:{self.exchange}"
        fields = {
            "alpha_id": req_id,
            "request_id": req_id,
            "response_stream": response_stream,
            "exchange": self.exchange,
            "tf": tf,
            "bars": str(bars),
            "symbols": ",".join(symbols),
            "symbols_json": json.dumps(symbols),
        }
        return stream, fields, response_stream, symbols

    async def _read_response(
        self,
        response_stream: str,
        symbols: list[str],
        tf: str,
    ) -> dict[tuple[str, str], list[dict]]:
        expected = set(symbols)
        collected: dict[tuple[str, str], list[dict]] = {}
        last_id = "0-0"
        deadline = time.monotonic() + self.timeout_sec

        while expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            block_ms = max(50, min(int(remaining * 1000), 1000))
            try:
                messages = await self._redis_call(
                    self.redis.xread,
                    {response_stream: last_id},
                    count=len(expected),
                    block=block_ms,
                )
            except RedisTimeoutError:
                continue
            if not messages:
                continue
            for _stream, entries in messages:
                for msg_id, fields in entries:
                    last_id = msg_id
                    symbol = fields.get("symbol", "")
                    if not symbol:
                        continue
                    try:
                        candles = json.loads(fields.get("candles", "[]"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        candles = []
                    collected[(symbol, fields.get("tf") or tf)] = candles
                    expected.discard(symbol)

        if expected:
            logger.warning(
                "[RUNNER-WARMUP] Partial MDS response on %s: %d/%d",
                response_stream,
                len(symbols) - len(expected),
                len(symbols),
            )
        return collected

    async def _redis_call(self, fn, *args, **kwargs):
        if getattr(self.redis, "_runner_inline_redis", False):
            return fn(*args, **kwargs)
        return await asyncio.to_thread(fn, *args, **kwargs)


class WarmupManager:
    def __init__(
        self,
        cache: SharedCandleCache,
        backend: RequestBackend,
        *,
        snapshot_reader=None,
        exchange: str = "binance",
        metrics: RunnerMetrics | None = None,
        max_concurrent_mds_requests: int = 3,
        max_mds_requests_per_minute: int = 20,
        max_symbols_per_mds_request: int = 10,
        request_timeout_sec: float = 60.0,
        response_cache_ttl_sec: float = 300.0,
        now_func: Callable[[], float] | None = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.cache = cache
        self.backend = backend
        self.snapshot_reader = snapshot_reader
        self.exchange = exchange
        self.metrics = metrics or RunnerMetrics()
        self.request_timeout_sec = float(request_timeout_sec)
        self.response_cache_ttl_sec = float(response_cache_ttl_sec)
        self._inflight: dict[str, asyncio.Task] = {}
        self._response_cache: dict[str, float] = {}
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrent_mds_requests)))
        self._max_per_minute = max(1, int(max_mds_requests_per_minute))
        self._max_symbols_per_request = max(1, int(max_symbols_per_mds_request))
        self._request_times: list[float] = []
        self._now = now_func or time.monotonic
        self._sleep = sleep_func or asyncio.sleep

    def collect_requirements(self, strategies: Iterable) -> dict[tuple[str, str], int]:
        result: dict[tuple[str, str], int] = {}
        for strategy in strategies:
            symbols = strategy.get_warmup_symbols()
            for tf in strategy.get_warmup_tfs():
                bars = int(strategy.get_warmup_bars(tf))
                retain_bars = int(getattr(strategy, "get_retain_bars", strategy.get_warmup_bars)(tf))
                retain_buffer_bars = int(getattr(strategy, "get_retain_buffer_bars", lambda _tf: 0)(tf))
                for symbol in symbols:
                    key = (symbol, tf)
                    result[key] = max(result.get(key, 0), bars)
                    self.cache.register_data_requirement(
                        symbol,
                        tf,
                        warmup_bars=bars,
                        retain_bars=retain_bars,
                        retain_buffer_bars=retain_buffer_bars,
                    )
        return result

    def strategy_ready(
        self,
        strategy,
        min_coverage: float = 0.90,
        max_age_sec: float | None = None,
    ) -> bool:
        symbols = strategy.get_warmup_symbols()
        alpha_id = getattr(getattr(strategy, "ctx", None), "alpha_id", None) or getattr(strategy, "alpha_id", "")
        if not symbols:
            if alpha_id:
                self.metrics.set_strategy_coverage(alpha_id, 1.0)
            return True
        ready_any = True
        coverages: list[float] = []
        for tf in strategy.get_warmup_tfs():
            bars = int(strategy.get_warmup_bars(tf))
            loaded, total, pct = self.cache.coverage(symbols, tf, bars, max_age_sec)
            required = max(1, int(total * min_coverage + 0.999999))
            ready_any = ready_any and loaded >= required and pct >= min_coverage
            coverages.append(pct)
        if alpha_id:
            self.metrics.set_strategy_coverage(alpha_id, min(coverages) if coverages else 1.0)
        return ready_any

    def missing_requirements(
        self,
        requirements: Mapping[tuple[str, str], int],
        max_age_sec: float | None = None,
    ) -> tuple[WarmupRequirement, ...]:
        missing = []
        for (symbol, tf), bars in requirements.items():
            ok = self.cache.has_required_bars(symbol, tf, bars)
            if ok and max_age_sec is not None:
                ok = self.cache.has_fresh_data(symbol, tf, bars, max_age_sec)
            if ok:
                self.metrics.inc("warmup_cache_hits_total")
            else:
                missing.append(WarmupRequirement(symbol, tf, int(bars)))
        return tuple(sorted(missing))

    def group_missing_by_bucket(
        self,
        requirements: Iterable[WarmupRequirement],
    ) -> dict[tuple[str, str], tuple[WarmupRequirement, ...]]:
        groups: dict[tuple[str, str], dict[tuple[str, str], WarmupRequirement]] = {}
        for req in requirements:
            group_key = (req.tf, bars_bucket(req.bars))
            symbol_key = (req.symbol, req.tf)
            current = groups.setdefault(group_key, {}).get(symbol_key)
            if current is None or req.bars > current.bars:
                groups[group_key][symbol_key] = req
        return {
            key: tuple(sorted(value.values()))
            for key, value in sorted(groups.items())
        }

    async def request_warmup(
        self,
        requirements: Mapping[tuple[str, str], int],
        max_age_sec: float | None = None,
    ) -> set[tuple[str, str]]:
        for (symbol, tf), bars in requirements.items():
            self.cache.register_data_requirement(symbol, tf, warmup_bars=int(bars), retain_bars=int(bars))
        missing = list(self.missing_requirements(requirements, max_age_sec))
        if not missing:
            return set()

        loaded = self._load_from_snapshots(missing, max_age_sec)
        remaining = [
            req for req in missing
            if (req.symbol, req.tf) not in loaded
            and not self._cache_satisfies(req, max_age_sec)
        ]
        if not remaining:
            return loaded

        for batch in self.group_missing_by_bucket(remaining).values():
            for chunk in self._chunk_requirements(batch):
                batch_loaded = await self._ensure_batch(chunk, max_age_sec)
                loaded.update(batch_loaded)
        return loaded

    def _chunk_requirements(
        self,
        requirements: tuple[WarmupRequirement, ...],
    ) -> tuple[tuple[WarmupRequirement, ...], ...]:
        if len(requirements) <= self._max_symbols_per_request:
            return (requirements,)
        return tuple(
            requirements[index:index + self._max_symbols_per_request]
            for index in range(0, len(requirements), self._max_symbols_per_request)
        )

    async def ensure_warmup(
        self,
        requirements: Mapping[tuple[str, str], int],
        max_age_sec: float | None = None,
    ) -> set[tuple[str, str]]:
        return await self.request_warmup(requirements, max_age_sec)

    def _load_from_snapshots(
        self,
        requirements: Iterable[WarmupRequirement],
        max_age_sec: float | None,
    ) -> set[tuple[str, str]]:
        loaded: set[tuple[str, str]] = set()
        if self.snapshot_reader is None:
            return loaded
        for req in requirements:
            if self._cache_satisfies(req, max_age_sec):
                continue
            candles = self.snapshot_reader.load(req.symbol, req.tf, req.bars)
            if not candles:
                continue
            for candle in candles:
                self.cache.upsert_candle(req.symbol, req.tf, candle)
            if self._cache_satisfies(req, max_age_sec):
                loaded.add((req.symbol, req.tf))
                self.metrics.inc("warmup_snapshot_hits_total")
        return loaded

    async def _ensure_batch(
        self,
        requirements: tuple[WarmupRequirement, ...],
        max_age_sec: float | None,
    ) -> set[tuple[str, str]]:
        if not requirements:
            return set()
        tf = requirements[0].tf
        bars = max(req.bars for req in requirements)
        symbols = sorted({req.symbol for req in requirements})
        key = request_key(self.exchange, tf, bars, symbols)

        if self._response_cache_valid(key, requirements, max_age_sec):
            self.metrics.inc("warmup_cache_hits_total", len(requirements))
            return {(req.symbol, req.tf) for req in requirements if self._cache_satisfies(req, max_age_sec)}

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._send_batch(key, requirements))
            self._inflight[key] = task
        try:
            loaded = await task
        finally:
            if task.done():
                self._inflight.pop(key, None)
        return {
            (req.symbol, req.tf)
            for req in requirements
            if (req.symbol, req.tf) in loaded or self._cache_satisfies(req, max_age_sec)
        }

    async def _send_batch(self, key: str, requirements: tuple[WarmupRequirement, ...]) -> set[tuple[str, str]]:
        async with self._semaphore:
            await self._wait_for_rate_limit()
            start = self._now()
            self.metrics.inc("warmup_mds_requests_total")
            self.metrics.inc("warmup_mds_symbols_requested_total", len(requirements))
            try:
                if getattr(self.backend, "handles_timeout", False):
                    result = await self.backend(requirements)
                else:
                    result = await asyncio.wait_for(self.backend(requirements), timeout=self.request_timeout_sec)
            except asyncio.TimeoutError:
                self.metrics.inc("warmup_timeouts_total")
                logger.warning(
                    "[RUNNER-WARMUP] MDS request timed out tf=%s bars=%d symbols=%d",
                    requirements[0].tf,
                    max(req.bars for req in requirements),
                    len(requirements),
                )
                return set()
            except Exception as exc:
                logger.warning("[RUNNER-WARMUP] MDS request failed: %s", exc)
                return set()
            finally:
                self.metrics.observe_duration(max(0.0, self._now() - start))

        loaded = self._apply_backend_result(requirements, result)
        if loaded:
            self._response_cache[key] = self._now() + self.response_cache_ttl_sec
        requested = {(req.symbol, req.tf) for req in requirements}
        if loaded and loaded != requested:
            self.metrics.inc("warmup_partial_ready_total")
            self.metrics.inc("warmup_timeouts_total")
        elif not loaded:
            self.metrics.inc("warmup_timeouts_total")
        return loaded

    async def _wait_for_rate_limit(self) -> None:
        while True:
            now = self._now()
            self._request_times = [t for t in self._request_times if now - t < 60.0]
            if len(self._request_times) < self._max_per_minute:
                self._request_times.append(now)
                return
            delay = max(0.0, 60.0 - (now - self._request_times[0]))
            await self._sleep(delay)

    def _apply_backend_result(
        self,
        requirements: tuple[WarmupRequirement, ...],
        result: RequestResult,
    ) -> set[tuple[str, str]]:
        if isinstance(result, set):
            return set(result)
        if not isinstance(result, dict):
            return set()

        loaded: set[tuple[str, str]] = set()
        req_by_key = {(req.symbol, req.tf): req for req in requirements}
        for key, candles in result.items():
            if isinstance(key, tuple):
                symbol, tf = str(key[0]), str(key[1])
            else:
                symbol, tf = str(key), requirements[0].tf
            if not isinstance(candles, list):
                continue
            for candle in candles:
                if isinstance(candle, dict):
                    self.cache.upsert_candle(symbol, tf, candle)
            req = req_by_key.get((symbol, tf))
            if req is not None and self.cache.has_required_bars(symbol, tf, req.bars):
                loaded.add((symbol, tf))
        return loaded

    def _response_cache_valid(
        self,
        key: str,
        requirements: tuple[WarmupRequirement, ...],
        max_age_sec: float | None,
    ) -> bool:
        expires_at = self._response_cache.get(key)
        if expires_at is None or expires_at <= self._now():
            self._response_cache.pop(key, None)
            return False
        return all(self._cache_satisfies(req, max_age_sec) for req in requirements)

    def _cache_satisfies(self, req: WarmupRequirement, max_age_sec: float | None) -> bool:
        ok = self.cache.has_required_bars(req.symbol, req.tf, req.bars)
        if ok and max_age_sec is not None:
            ok = self.cache.has_fresh_data(req.symbol, req.tf, req.bars, max_age_sec)
        return ok
