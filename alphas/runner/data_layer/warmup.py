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

_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def _tf_ms(tf: str) -> int:
    return _TF_MINUTES.get(tf, 1) * 60 * 1000


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
        # Hash the FULL key (exchange|tf|bars|symbol-digest), not just its last
        # segment — `key.split('|')[-1]` was only the symbol digest, so two
        # concurrent requests for different tf/bars over the same symbol set
        # collided on the same response_stream. Redis XREAD doesn't consume
        # entries for other readers, so both requests' _read_response loops
        # would race-read each other's response and exit early on whichever
        # entry arrived first (matched by symbol only) — silently losing
        # whichever tf's data lost the race. Any alpha with an `htf` on the
        # same symbol set as its primary `tf` (e.g. bangoc-v2.2, short-btc-v1)
        # hits this.
        req_id = f"{self.runner_id}:warmup:{hashlib.sha1(key.encode('utf-8')).hexdigest()}"
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
        self._excluded_symbols: set[str] = set()
        self._requirements: dict[tuple[str, str], int] = {}
        self._warmup_baseline_ts: dict[str, int] = {}
        self._strategies = None

    def collect_requirements(self, strategies: Iterable) -> dict[tuple[str, str], int]:
        self._strategies = strategies
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
        self._requirements = result
        return result

    def strategy_ready(
        self,
        strategy,
        min_coverage: float = 0.90,
        max_age_sec: float | None = None,
    ) -> bool:
        symbols = [s for s in strategy.get_warmup_symbols() if s not in self._excluded_symbols]
        alpha_id = getattr(getattr(strategy, "ctx", None), "alpha_id", None) or getattr(strategy, "alpha_id", "")
        if not symbols:
            if alpha_id:
                self.metrics.set_strategy_coverage(alpha_id, 1.0)
            return True
        ready_any = True
        coverages: list[float] = []
        for tf in strategy.get_warmup_tfs():
            bars = int(strategy.get_warmup_bars(tf))
            loaded = sum(
                1
                for symbol in symbols
                if self._cache_satisfies_symbol(symbol, tf, bars, max_age_sec)
            )
            total = len(symbols)
            pct = loaded / total if total else 0.0
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
            if symbol in self._excluded_symbols:
                continue
            ok = self._cache_satisfies_symbol(symbol, tf, bars, max_age_sec)
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
        stop_when_ready_pct: float | None = None,
    ) -> set[tuple[str, str]]:
        for (symbol, tf), bars in requirements.items():
            self.cache.register_data_requirement(symbol, tf, warmup_bars=int(bars), retain_bars=int(bars))
        missing = list(self.missing_requirements(requirements, max_age_sec))
        if not missing:
            return set()

        loaded = self._load_from_snapshots(missing, max_age_sec)
        if self._all_strategies_ready(stop_when_ready_pct, max_age_sec):
            logger.info(
                "[RUNNER-WARMUP] Coverage target reached after snapshot/cache restore; skipping direct fill"
            )
            return loaded
        remaining = [
            req for req in missing
            if (req.symbol, req.tf) not in loaded
            and not self._cache_satisfies(req, max_age_sec)
        ]
        if not remaining:
            return loaded

        all_chunks: list[tuple[WarmupRequirement, ...]] = []
        for batch in self.group_missing_by_bucket(remaining).values():
            all_chunks.extend(self._chunk_requirements(batch))
        if all_chunks:
            tasks = [
                asyncio.create_task(self._ensure_batch(chunk, max_age_sec))
                for chunk in all_chunks
            ]
            for coro in asyncio.as_completed(tasks):
                batch_loaded = await coro
                loaded.update(batch_loaded)
                if self._all_strategies_ready(stop_when_ready_pct, max_age_sec):
                    logger.info(
                        "[RUNNER-WARMUP] Coverage target reached; skipping remaining direct fill"
                    )
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    return loaded
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

    def _all_strategies_ready(
        self,
        min_coverage: float | None,
        max_age_sec: float | None,
    ) -> bool:
        if min_coverage is None or self._strategies is None:
            return False
        strategies = list(self._strategies)
        if not strategies:
            return False
        return all(
            self.strategy_ready(strategy, min_coverage=min_coverage, max_age_sec=max_age_sec)
            for strategy in strategies
        )

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
            tf = requirements[0].tf
            bars = max(req.bars for req in requirements)
            self.metrics.inc("warmup_mds_requests_total")
            self.metrics.inc("warmup_mds_symbols_requested_total", len(requirements))
            logger.info(
                "[RUNNER-WARMUP] Requesting MDS tf=%s bars=%d symbols=%d",
                tf,
                bars,
                len(requirements),
            )
            try:
                if getattr(self.backend, "handles_timeout", False):
                    result = await self.backend(requirements)
                else:
                    result = await asyncio.wait_for(self.backend(requirements), timeout=self.request_timeout_sec)
            except asyncio.TimeoutError:
                self.metrics.inc("warmup_timeouts_total")
                logger.warning(
                    "[RUNNER-WARMUP] MDS request timed out tf=%s bars=%d symbols=%d",
                    tf,
                    bars,
                    len(requirements),
                )
                return set()
            except Exception as exc:
                logger.warning("[RUNNER-WARMUP] MDS request failed: %s", exc)
                return set()
            finally:
                self.metrics.observe_duration(max(0.0, self._now() - start))

        loaded = self._apply_backend_result(requirements, result)
        logger.info(
            "[RUNNER-WARMUP] MDS response applied tf=%s bars=%d loaded=%d/%d duration=%.2fs",
            requirements[0].tf,
            max(req.bars for req in requirements),
            len(loaded),
            len(requirements),
            self.metrics.warmup_request_duration_sec[-1] if self.metrics.warmup_request_duration_sec else 0.0,
        )
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
            if req is not None and self._cache_satisfies(req, None):
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
        return self._cache_satisfies_symbol(req.symbol, req.tf, req.bars, max_age_sec)

    def _cache_satisfies_symbol(
        self,
        symbol: str,
        tf: str,
        bars: int,
        max_age_sec: float | None,
    ) -> bool:
        ok = self.cache.has_required_bars(symbol, tf, bars)
        if ok and max_age_sec is not None:
            ok = self.cache.has_fresh_data(symbol, tf, bars, max_age_sec)
        if ok:
            ok = self.cache.verify_no_gaps(symbol, tf).is_clean
        return ok

    def _verify_timestamp_sync(self, sync_tolerance_candles: int = 1, _depth: int = 0) -> bool:
        if _depth > 2:
            return False
        for tf in self._get_required_tfs():
            latest_timestamps: dict[str, int] = {}
            for symbol in self.cache.get_symbols_with_data(tf):
                if symbol in self._excluded_symbols:
                    continue
                ts = self.cache.get_latest_timestamp(symbol, tf)
                if ts is not None:
                    latest_timestamps[symbol] = ts

            if not latest_timestamps:
                continue

            max_ts = max(latest_timestamps.values())
            min_ts = min(latest_timestamps.values())
            tolerance_ms = _tf_ms(tf) * sync_tolerance_candles

            if max_ts - min_ts > tolerance_ms:
                stale = [s for s, ts in latest_timestamps.items()
                         if max_ts - ts > tolerance_ms]
                logger.warning(
                    "[WARMUP-SYNC] Timestamp spread %dms > %dms for %s. Re-reading %d stale symbol(s): %s",
                    max_ts - min_ts, tolerance_ms, tf, len(stale), stale[:10],
                )
                if self.snapshot_reader:
                    for symbol in stale:
                        bars = self._requirements.get((symbol, tf), 0)
                        candles = self.snapshot_reader.load(symbol, tf, bars)
                        if candles:
                            for candle in candles:
                                self.cache.upsert_candle(symbol, tf, candle)
                return self._verify_timestamp_sync(sync_tolerance_candles, _depth + 1)

        return True

    def _classify_symbols(
        self,
        signals: dict[str, "ReadySignal"],
        min_warmup_coverage_pct: float = 0.60,
    ) -> None:
        from runner.data_layer.mds_ready import ReadySignal

        insufficient_excluded: list[tuple[str, str]] = []
        low_coverage_excluded: list[tuple[str, str, float]] = []
        partial_accepted: list[tuple[str, str, float]] = []
        for tf, signal in signals.items():
            for symbol in signal.insufficient_symbols:
                self._excluded_symbols.add(symbol)
                insufficient_excluded.append((symbol, tf))
            for symbol, pct in signal.partial_symbols.items():
                if pct >= min_warmup_coverage_pct:
                    partial_accepted.append((symbol, tf, pct))
                else:
                    self._excluded_symbols.add(symbol)
                    low_coverage_excluded.append((symbol, tf, pct))

        if insufficient_excluded:
            logger.warning(
                "[WARMUP-CLASSIFY] Excluding insufficient-history symbols: count=%d sample=%s",
                len(insufficient_excluded),
                insufficient_excluded[:10],
            )
        if low_coverage_excluded:
            logger.warning(
                "[WARMUP-CLASSIFY] Excluding low-coverage symbols: count=%d sample=%s",
                len(low_coverage_excluded),
                [(s, tf, round(pct, 3)) for s, tf, pct in low_coverage_excluded[:10]],
            )
        if partial_accepted:
            logger.info(
                "[WARMUP-CLASSIFY] Accepted partial warmup symbols: count=%d sample=%s",
                len(partial_accepted),
                [(s, tf, round(pct, 3)) for s, tf, pct in partial_accepted[:10]],
            )

    def _get_required_tfs(self) -> list[str]:
        return sorted(set(tf for _sym, tf in self._requirements))

    @property
    def excluded_symbols(self) -> set[str]:
        return set(self._excluded_symbols)

    @property
    def warmup_baseline_ts(self) -> dict[str, int]:
        return dict(self._warmup_baseline_ts)

    async def wait_for_mds_ready(
        self,
        ready_watcher: "MDSReadyWatcher",
        required_tfs: list[str],
        timeout_sec: float = 900.0,
    ) -> dict[str, "ReadySignal"]:
        signals = await ready_watcher.wait_for_ready(required_tfs, timeout_sec)
        return signals

    async def run_synced_warmup(
        self,
        ready_watcher: "MDSReadyWatcher" | None = None,
        mds_ready_timeout_sec: float = 900.0,
        min_warmup_coverage_pct: float = 0.60,
        sync_tolerance_candles: int = 1,
        max_staleness_sec: float | None = None,
    ) -> bool:
        required_tfs = self._get_required_tfs()

        if ready_watcher and required_tfs:
            signals = await self.wait_for_mds_ready(
                ready_watcher, required_tfs, mds_ready_timeout_sec
            )
            if len(signals) < len(required_tfs):
                logger.warning("[WARMUP] MDS ready signals incomplete — falling back to direct warmup")
            else:
                self._classify_symbols(signals, min_warmup_coverage_pct)

        active_requirements = {
            key: bars
            for key, bars in self._requirements.items()
            if key[0] not in self._excluded_symbols
        }
        await self.request_warmup(
            active_requirements,
            max_age_sec=max_staleness_sec,
            stop_when_ready_pct=min_warmup_coverage_pct,
        )

        if not self._verify_timestamp_sync(sync_tolerance_candles):
            logger.warning("[WARMUP] Timestamp sync check failed — some symbols may have stale data")

        for tf in required_tfs:
            max_ts = 0
            for symbol in self.cache.get_symbols_with_data(tf):
                if symbol in self._excluded_symbols:
                    continue
                ts = self.cache.get_latest_timestamp(symbol, tf)
                if ts and ts > max_ts:
                    max_ts = ts
            if max_ts > 0:
                self.cache.set_warmup_baseline(tf, max_ts)
                self._warmup_baseline_ts[tf] = max_ts

        for tf in required_tfs:
            reports = self.cache.verify_all_no_gaps(tf)
            gapped = [r for r in reports if not r.is_clean and r.symbol not in self._excluded_symbols]
            if gapped:
                logger.warning(
                    "[WARMUP-GAP-CHECK] %d/%d symbols have gaps in %s",
                    len(gapped), len(reports), tf,
                )
                for report in gapped[:5]:
                    logger.debug(
                        "[WARMUP-GAP-CHECK] %s %s: %d gaps, %d missing ranges",
                        report.symbol, report.tf, report.gap_count,
                        len(report.missing_ranges),
                    )

        return True

    def get_required_tfs(self) -> list[str]:
        return self._get_required_tfs()
