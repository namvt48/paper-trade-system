from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import importlib
import logging
import os
from pathlib import Path
import queue
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import QueueHandler, QueueListener

import redis

from runner.config import AlphaConfig, load_runner_config
from runner.data_layer.cache import SharedCandleCache
from runner.data_layer.pubsub import DataEvent, SharedPubSubManager
from runner.data_layer.snapshot import SnapshotReader
from runner.data_layer.warmup import MDSWarmupBackend, WarmupManager
from runner.lease import LeaseManager
from runner.metrics import RunnerMetrics
from runner.metrics_http import MetricsServer
from runner.reconcile.state import StrategyRuntimeState
from runner.shared_panel_feature_cache import SharedPanelFeatureCache
from runner.signal.dispatcher import SignalDispatcher
from runner.strategy.context import PriceAlertProxy, StrategyContext
from runner.strategy.registry import StrategyRegistry


logger = logging.getLogger(__name__)
_LOG_QUEUE_HANDLER: QueueHandler | None = None
_LOG_QUEUE_LISTENER: QueueListener | None = None


def _translate_channels(channels: list[str], exchange: str) -> list[str]:
    result = []
    for ch in channels:
        if ch.startswith("kline:") and ":" not in ch[6:]:
            tf = ch[6:]
            result.append(f"kline:{exchange}:{tf}")
        else:
            result.append(ch)
    return result


def _tf_set_from_strategy(registry: StrategyRegistry, alpha: AlphaConfig) -> list[str]:
    try:
        cls = registry.get_class(alpha.strategy)
        channels = cls.get_required_channels(alpha.params)
        return sorted(ch.replace("kline:", "") for ch in channels)
    except Exception:
        return ["1m"]


def _tf_to_1m_multiplier(tf: str) -> int:
    unit = tf[-1]
    value = int(tf[:-1])
    if unit == "m":
        return value
    if unit == "h":
        return value * 60
    if unit == "d":
        return value * 1440
    return 1


def _parquet_restore_plan(
    requirements: dict[tuple[str, str], int],
) -> tuple[set[str], dict[str, int], dict[tuple[str, str], int]]:
    symbols: set[str] = set()
    tail_rows_by_symbol: dict[str, int] = {}
    for (symbol, tf), bars in requirements.items():
        symbols.add(symbol)
        try:
            multiplier = _tf_to_1m_multiplier(tf)
        except Exception:
            multiplier = 1
        tail_rows = int(bars) * multiplier + max(multiplier * 2, 10)
        tail_rows_by_symbol[symbol] = max(tail_rows_by_symbol.get(symbol, 0), tail_rows)
    return symbols, tail_rows_by_symbol, requirements


def shutdown_logging() -> None:
    global _LOG_QUEUE_HANDLER, _LOG_QUEUE_LISTENER
    if _LOG_QUEUE_HANDLER is not None:
        root_logger = logging.getLogger()
        root_logger.removeHandler(_LOG_QUEUE_HANDLER)
        _LOG_QUEUE_HANDLER.close()
        _LOG_QUEUE_HANDLER = None
    listener = _LOG_QUEUE_LISTENER
    if listener is None:
        return
    listener.stop()
    for handler in getattr(listener, "handlers", ()):
        handler.close()
    _LOG_QUEUE_LISTENER = None


def setup_logging() -> None:
    global _LOG_QUEUE_HANDLER, _LOG_QUEUE_LISTENER
    shutdown_logging()
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    for handler in handlers:
        handler.setFormatter(formatter)

    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    queue_handler = QueueHandler(log_queue)
    queue_handler.setLevel(level)
    listener = QueueListener(log_queue, *handlers, respect_handler_level=True)
    listener.start()
    _LOG_QUEUE_HANDLER = queue_handler
    _LOG_QUEUE_LISTENER = listener

    logging.basicConfig(
        level=level, handlers=[queue_handler], format="%(message)s", force=True
    )


atexit.register(shutdown_logging)


def build_registry(modules: tuple[str, ...]) -> StrategyRegistry:
    registry = StrategyRegistry()
    for module_name in modules:
        module = importlib.import_module(module_name)
        register = getattr(module, "register", None)
        if callable(register):
            register(registry)
    return registry


async def _noop_backend(requirements):
    return set()


# Per-timeframe staleness floor for the /health "silent alpha" check --
# deliberately coarser than the U4 per-event watchdog thresholds, since
# this flags an alpha that hasn't processed *any* event in a very long
# time (e.g. 2026-07-16: 5 daily alphas produced zero events for 12+
# hours), not a single slow scan.
_STALE_TF_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_STALE_CANDLES = 2


def _alpha_stale_threshold_sec(strategy) -> float:
    get_warmup_tfs = getattr(strategy, "get_warmup_tfs", None)
    tfs = get_warmup_tfs() if callable(get_warmup_tfs) else None
    tf_ms_values = [_STALE_TF_MS.get(tf, 60_000) for tf in (tfs or [])] or [60_000]
    return max(60.0, (min(tf_ms_values) / 1000.0) * _STALE_CANDLES)


def _is_alpha_stale(strategy, metrics: RunnerMetrics, now: float) -> bool:
    # Metrics/health introspection must never crash the runner over an
    # incomplete strategy-like object (e.g. a test double) -- losing
    # observability is exactly the failure mode this exists to prevent.
    alpha_id = getattr(strategy.ctx, "alpha_id", None) or getattr(
        strategy, "alpha_id", None
    )
    last_ts = metrics.last_event_ts_by_alpha.get(alpha_id)
    if last_ts is None:
        return False  # never had a chance to process an event yet (e.g. just started)
    return (now - last_ts) > _alpha_stale_threshold_sec(strategy)


def runner_metrics_snapshot(
    metrics: RunnerMetrics,
    cfg,
    strategies,
    lease,
    cache: SharedCandleCache | None = None,
    panel_feature_cache: SharedPanelFeatureCache | None = None,
) -> dict:
    snapshot = metrics.snapshot()
    now = time.time()
    snapshot.update(
        {
            "runner_id": cfg.runner_id,
            "signal_stream": cfg.signal_stream,
            "shadow_mode": cfg.shadow_mode,
            "strategies_active": sum(
                1 for s in strategies if s.ctx.state.ready and s.ctx.state.lease_valid
            ),
            "strategies_suspended": sum(
                1
                for s in strategies
                if not s.ctx.state.ready or not s.ctx.state.lease_valid
            ),
            "lease_owner": {},
            "last_event_age_sec": {
                aid: now - ts for aid, ts in metrics.last_event_ts_by_alpha.items()
            },
            "stale_alphas": [
                getattr(s.ctx, "alpha_id", None) or getattr(s, "alpha_id", None)
                for s in strategies
                if _is_alpha_stale(s, metrics, now)
            ],
        }
    )
    if lease is not None:
        for strategy in strategies:
            try:
                owner = lease.redis_client.get(lease.key(strategy.ctx.alpha_id))
            except Exception:
                owner = None
            snapshot["lease_owner"][strategy.ctx.alpha_id] = owner
    if cache is not None:
        cache_rows = [row.__dict__ for row in cache.stats()]
        snapshot["cache"] = {
            "keys": cache_rows,
            "loaded_bars_total": sum(row["loaded_bars"] for row in cache_rows),
            "retained_bars_total": sum(row["retain_bars"] for row in cache_rows),
            "trim_count_total": sum(row["trim_count"] for row in cache_rows),
        }
    if panel_feature_cache is not None:
        snapshot["panel_feature_cache"] = panel_feature_cache.snapshot()
    return snapshot


async def renew_strategy_leases(
    lease: LeaseManager,
    strategies,
    interval_sec: float,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(interval_sec))
            break
        except asyncio.TimeoutError:
            pass

        any_valid = False
        for strategy in strategies:
            alpha_id = strategy.ctx.alpha_id
            try:
                ok = (
                    lease.renew(alpha_id)
                    if lease.is_valid(alpha_id)
                    else lease.acquire(alpha_id)
                )
            except Exception:
                ok = False
            strategy.ctx.state.lease_valid = bool(ok)
            any_valid = any_valid or bool(ok)
        if strategies and not any_valid:
            break


def refresh_strategy_readiness(strategy, max_age_sec: float | None = None) -> None:
    symbols = strategy.get_warmup_symbols()
    if not symbols:
        strategy.ctx.state.ready = True
        return

    ready = True
    for tf in strategy.get_warmup_tfs():
        bars = int(strategy.get_warmup_bars(tf))
        ready = (
            strategy.ctx.update_readiness(symbols, tf, bars, max_age_sec=max_age_sec)
            and ready
        )
    strategy.ctx.state.ready = ready


def _duration_ms(start: float, end: float | None = None) -> float:
    return ((end if end is not None else time.perf_counter()) - start) * 1000.0


def _payload_int(payload: dict | None, key: str) -> int | None:
    if not payload:
        return None
    value = payload.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Per-timeframe ceiling on a single event's processing time. If scan() (or
# any other per-event work) takes longer than this -- e.g. an unbounded
# I/O call or a thread-pool-starved compute step -- the event is abandoned
# and logged rather than silently blocking this strategy's loop forever.
# 2026-07-16 incident: 5 daily alphas' loops each got stuck on one event
# and never processed another, including the next day's candle close,
# until the whole runner was restarted. Thresholds per .agents/PLAN.md.
_EVENT_TIMEOUT_SEC: dict[str, float] = {
    "1d": 120.0,
    "4h": 120.0,
    "1h": 90.0,
    "15m": 60.0,
}
_DEFAULT_EVENT_TIMEOUT_SEC = 60.0


def _event_timeout_sec(event: DataEvent) -> float:
    return _EVENT_TIMEOUT_SEC.get(event.tf, _DEFAULT_EVENT_TIMEOUT_SEC)


async def handle_strategy_event(strategy, event: DataEvent) -> dict:
    timings = {
        "readiness_ms": 0.0,
        "on_candle_ms": 0.0,
        "should_scan_ms": 0.0,
        "scan_ms": 0.0,
        "manage_ms": 0.0,
        "scanned": False,
        "ready": bool(getattr(strategy.ctx.state, "ready", False)),
    }
    if event.kind == "kline" and event.symbol and event.tf:
        started = time.perf_counter()
        refresh_strategy_readiness(strategy)
        timings["readiness_ms"] = _duration_ms(started)

        started = time.perf_counter()
        await strategy.on_candle(event.symbol, event.tf)
        timings["on_candle_ms"] = _duration_ms(started)

        started = time.perf_counter()
        should_scan = strategy.should_scan_after_event(
            event.kind, event.symbol, event.tf
        )
        timings["should_scan_ms"] = _duration_ms(started)

        if should_scan:
            started = time.perf_counter()
            await strategy.scan()
            timings["scan_ms"] = _duration_ms(started)
            timings["scanned"] = True
        timings["ready"] = bool(getattr(strategy.ctx.state, "ready", False))
        return timings

    if event.kind == "price_alert" and event.symbol:
        payload = event.payload or {}
        price = float(
            payload.get("price", payload.get("bid", payload.get("ask", 0.0))) or 0.0
        )
        side = str(payload.get("side", ""))
        started = time.perf_counter()
        await strategy.on_price_alert(event.symbol, price, side)
        timings["on_candle_ms"] = _duration_ms(started)

        started = time.perf_counter()
        should_scan = strategy.should_scan_after_event(event.kind, event.symbol, None)
        timings["should_scan_ms"] = _duration_ms(started)

        if should_scan:
            started = time.perf_counter()
            await strategy.scan()
            timings["scan_ms"] = _duration_ms(started)
            timings["scanned"] = True
        timings["ready"] = bool(getattr(strategy.ctx.state, "ready", False))
        return timings

    if event.kind == "symbols":
        payload = event.payload or {}
        strategy.ctx.live_tradable_symbols = set(payload.get("symbols") or [])

        started = time.perf_counter()
        await strategy.scan()
        timings["scan_ms"] = _duration_ms(started)
        timings["scanned"] = True
        timings["ready"] = bool(getattr(strategy.ctx.state, "ready", False))
    return timings


async def run_strategy_event_loop(
    strategy,
    queue: asyncio.Queue[DataEvent],
    stop_event: asyncio.Event,
    metrics: RunnerMetrics | None = None,
    scan_semaphore: asyncio.Semaphore | None = None,
) -> None:
    """Process one strategy queue while recording bounded admission and scan latency."""
    while not stop_event.is_set():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        try:
            started = time.perf_counter()
            queue_wait_ms = 0.0
            if event.received_monotonic > 0:
                queue_wait_ms = max(0.0, (started - event.received_monotonic) * 1000.0)
            event_timeout_sec = _event_timeout_sec(event)
            semaphore_wait_started = time.perf_counter()
            waiter_registered = metrics is not None and scan_semaphore is not None
            if waiter_registered and metrics is not None:
                metrics.scan_wait_started()
            try:
                # A universe refresh fans out to every alpha's queue at
                # once; without a ceiling here, all of them would call
                # handle_strategy_event (and its asyncio.to_thread compute
                # work) simultaneously, overwhelming the runner's small
                # shared thread pool in one burst (2026-07-16 incident;
                # single-flight above already collapses duplicate builds
                # within one (tf, universe), this bounds arrival *rate*
                # across all of them). No-op when no semaphore is passed
                # (existing callers/tests keep today's behavior).
                async with scan_semaphore or contextlib.nullcontext():
                    semaphore_wait_ms = _duration_ms(semaphore_wait_started)
                    if waiter_registered and metrics is not None:
                        metrics.scan_wait_finished()
                        waiter_registered = False
                    timings = await asyncio.wait_for(
                        handle_strategy_event(strategy, event),
                        timeout=event_timeout_sec,
                    )
            except asyncio.TimeoutError:
                if metrics is not None:
                    metrics.inc_scan_timeout(strategy.alpha_id)
                logger.warning(
                    "[STRATEGY] scan timeout alpha=%s event=%s channel=%s tf=%s "
                    "elapsed_sec=%.1f threshold_sec=%.1f -- abandoning this event, "
                    "moving on to the next one",
                    strategy.alpha_id,
                    event.kind,
                    event.channel,
                    event.tf or "-",
                    _duration_ms(started) / 1000.0,
                    event_timeout_sec,
                    extra={"alpha_id": strategy.alpha_id},
                )
                continue
            finally:
                if waiter_registered and metrics is not None:
                    metrics.scan_wait_finished()
            if metrics is not None:
                metrics.mark_event_processed(strategy.alpha_id, time.time())
            total_ms = _duration_ms(started)
            if metrics is not None:
                metrics.observe_event(
                    kind=event.kind,
                    queue_wait_ms=queue_wait_ms,
                    semaphore_wait_ms=semaphore_wait_ms,
                    scan_ms=timings.get("scan_ms", 0.0),
                    total_ms=total_ms,
                    scanned=bool(timings.get("scanned", False)),
                )
            data_open_ms = _payload_int(event.payload, "open_time")
            data_close_ms = _payload_int(event.payload, "close_time")
            data_age_ms = 0
            if data_close_ms is not None:
                data_age_ms = max(0, int(time.time() * 1000) - data_close_ms)
            log_fn = logger.info if timings.get("scanned", False) else logger.debug
            log_fn(
                "[ALPHA_TIMING] alpha=%s event=%s channel=%s symbol=%s tf=%s "
                "data_open_ms=%s data_close_ms=%s data_age_ms=%s "
                "queue_wait_ms=%.3f readiness_ms=%.3f on_data_ms=%.3f "
                "should_scan_ms=%.3f scan_ms=%.3f total_ms=%.3f scanned=%s ready=%s",
                strategy.alpha_id,
                event.kind,
                event.channel,
                event.symbol or "-",
                event.tf or "-",
                data_open_ms if data_open_ms is not None else "-",
                data_close_ms if data_close_ms is not None else "-",
                data_age_ms if data_close_ms is not None else "-",
                queue_wait_ms,
                timings.get("readiness_ms", 0.0),
                timings.get("on_candle_ms", 0.0),
                timings.get("should_scan_ms", 0.0),
                timings.get("scan_ms", 0.0),
                total_ms,
                timings.get("scanned", False),
                timings.get("ready", False),
                extra={"alpha_id": strategy.alpha_id},
            )
        except Exception:
            logger.exception(
                "[STRATEGY] Error processing event for alpha=%s",
                strategy.alpha_id,
                extra={"alpha_id": strategy.alpha_id},
            )
        finally:
            queue.task_done()


async def run_strategy_manage_loop(
    strategy,
    stop_event: asyncio.Event,
    interval_sec: float = 5.0,
) -> None:
    while not stop_event.is_set():
        await strategy.manage_positions()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            continue


async def run_price_alert_reconcile_loop(
    strategies,
    pubsub,
    stop_event: asyncio.Event,
    interval_sec: float = 5.0,
) -> None:
    """Keep runner-side price_alert: subscriptions in sync with held positions.

    Each strategy's ``ctx.price_alerts`` proxy records the symbols it has open
    positions on (via ``sync``). This loop subscribes the runner to the concrete
    ``price_alert:{exchange}:{symbol}`` channels MDS publishes ticks on, and
    unsubscribes channels the strategy no longer needs. The strategy's own
    ``manage_positions``/``_persist_positions`` already publish the MDS
    registration through the proxy; this loop only owns the pubsub subscribe side.
    """
    if pubsub is None:
        return
    subscribed: dict[str, set[str]] = {}
    while not stop_event.is_set():
        try:
            for strategy in strategies:
                proxy = getattr(strategy.ctx, "price_alerts", None)
                if proxy is None:
                    continue
                desired = proxy.active_prefixed_channels()
                alpha_id = strategy.alpha_id
                current = subscribed.get(alpha_id, set())
                for channel in desired - current:
                    await pubsub.subscribe(channel, alpha_id)
                for channel in current - desired:
                    await pubsub.unsubscribe(channel, alpha_id)
                subscribed[alpha_id] = desired
        except Exception:
            logger.exception(
                "[RUNNER] Price-alert reconcile error",
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            continue


async def periodic_gap_check(
    cache: SharedCandleCache,
    strategies,
    interval_sec: float = 300.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    last_warning_by_tf: dict[
        str, tuple[int, tuple[tuple[str, int, tuple[tuple[int, int], ...]], ...]]
    ] = {}
    while stop_event is None or not stop_event.is_set():
        if stop_event:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
                break
            except asyncio.TimeoutError:
                pass

        tfs = set()
        for strategy in strategies:
            tfs.update(strategy.get_warmup_tfs())

        for tf in tfs:
            reports = cache.verify_all_no_gaps(tf)
            gapped = [r for r in reports if not r.is_clean]
            if gapped:
                sample_reports = sorted(gapped, key=lambda r: r.symbol)[:5]
                sample = tuple(
                    (r.symbol, r.gap_count, r.missing_ranges[:2])
                    for r in sample_reports
                )
                warning_key = (len(gapped), sample)
                sample_text = ", ".join(
                    f"{symbol}:{gap_count}" for symbol, gap_count, _missing in sample
                )
                if last_warning_by_tf.get(tf) == warning_key:
                    logger.debug(
                        "[GAP-CHECK] repeated %d symbols have %s gaps (sample=%s)",
                        len(gapped),
                        tf,
                        sample_text,
                    )
                    continue
                else:
                    last_warning_by_tf[tf] = warning_key
                logger.warning(
                    "[GAP-CHECK] %d symbols have %s gaps (sample=%s)",
                    len(gapped),
                    tf,
                    sample_text,
                )
                continue
            last_warning_by_tf.pop(tf, None)


def install_stop_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass


async def run(
    config_path: str,
    dry_run: bool = False,
    *,
    keep_running: bool = False,
    metrics_host: str = "0.0.0.0",
    metrics_port: int = 9091,
) -> dict:
    logger.info(
        "[RUNNER] Loading config=%s dry_run=%s metrics=%s:%s",
        config_path,
        dry_run,
        metrics_host,
        metrics_port,
    )
    cfg = load_runner_config(config_path)
    compute_executor = None
    if cfg.compute_workers > 0:
        compute_executor = ThreadPoolExecutor(
            max_workers=cfg.compute_workers,
            thread_name_prefix="alpha-compute",
        )
        asyncio.get_running_loop().set_default_executor(compute_executor)
        logger.info("[RUNNER] Compute thread pool workers=%d", cfg.compute_workers)
    logger.info(
        "[RUNNER] Config loaded runner_id=%s stream=%s shadow=%s alphas=%d modules=%d",
        cfg.runner_id,
        cfg.signal_stream,
        cfg.shadow_mode,
        len(cfg.alphas),
        len(cfg.modules),
    )
    allowed_alpha_ids = {alpha.alpha_id for alpha in cfg.alphas}
    registry = build_registry(cfg.modules)
    cache = SharedCandleCache(data_max_candles_floor=cfg.cache.min_retain_bars)
    panel_feature_cache = SharedPanelFeatureCache()
    metrics = RunnerMetrics()
    # Bounds how many alphas' handle_strategy_event calls (and their
    # to_thread compute work) run at once across the whole runner. A
    # universe refresh fans out to every alpha's queue simultaneously;
    # without this, all of them would hit the shared compute thread pool
    # in one burst (2026-07-16 incident, see .agents/PLAN.md U5).
    scan_semaphore = asyncio.Semaphore(max(1, cfg.compute_workers))
    redis_client = None
    mds_client = None

    if dry_run:
        lease = None
        dispatcher = None
        pubsub = None
        warmup_backend = _noop_backend
        snapshot_reader = None
    else:
        redis_client = redis.from_url(cfg.redis_url, decode_responses=True)
        # mds_client is only ever used for bounded request/response calls
        # (warmup, snapshot reads, funding reads) -- never for the
        # long-lived pubsub subscription, which SharedPubSubManager opens
        # on its own separate redis.asyncio connection. A dead/unresponsive
        # mds-redis connection must therefore raise instead of blocking a
        # shared compute-pool thread forever (2026-07-16 incident: an
        # unbounded funding read permanently starved 5 daily alphas).
        mds_client = redis.from_url(
            cfg.mds_redis_url or cfg.redis_url,
            decode_responses=True,
            socket_timeout=cfg.mds_redis_socket_timeout_sec,
            socket_connect_timeout=cfg.mds_redis_socket_timeout_sec,
        )
        lease = LeaseManager(redis_client, cfg.runner_id, cfg.lease_ttl_sec)
        dispatcher = SignalDispatcher(
            redis_client, cfg.signal_stream, lease, metrics=metrics
        )
        pubsub = SharedPubSubManager(mds_client, cache, cfg.data_queue_maxsize)
        warmup_backend = MDSWarmupBackend(
            mds_client,
            cfg.mds_exchange,
            cfg.runner_id,
            cfg.warmup.request_timeout_sec,
        )
        snapshot_reader = SnapshotReader(mds_client, cfg.mds_exchange)

    # --- Read configs from Redis and claim alphas ---
    if dry_run:
        claimed_configs = [
            {
                "alpha_id": a.alpha_id,
                "strategy": a.strategy,
                "version": a.version,
                "params": a.params,
                "tf_set": _tf_set_from_strategy(registry, a),
            }
            for a in cfg.alphas
        ]
    else:
        from runner.config_sync import read_alpha_configs_from_redis
        from runner.alpha_claim import claim_alpha_groups

        all_configs = read_alpha_configs_from_redis(redis_client)
        if not all_configs:
            logger.info("[RUNNER] No alpha configs in Redis, falling back to YAML")
            all_configs = [
                {
                    "alpha_id": a.alpha_id,
                    "strategy": a.strategy,
                    "version": a.version,
                    "params": a.params,
                    "tf_set": _tf_set_from_strategy(registry, a),
                }
                for a in cfg.alphas
            ]
        else:
            all_configs = [
                c for c in all_configs if c.get("alpha_id") in allowed_alpha_ids
            ]
            if not all_configs:
                logger.info(
                    "[RUNNER] Redis configs exist but none match this runner config; falling back to YAML"
                )
                all_configs = [
                    {
                        "alpha_id": a.alpha_id,
                        "strategy": a.strategy,
                        "version": a.version,
                        "params": a.params,
                        "tf_set": _tf_set_from_strategy(registry, a),
                    }
                    for a in cfg.alphas
                ]

        claimed_configs = claim_alpha_groups(
            redis_client,
            cfg.runner_id,
            all_configs,
            max_alphas_per_runner=cfg.max_alphas_per_runner,
            ttl_sec=cfg.lease_ttl_sec,
        )

    # --- Instantiate strategies from claimed configs ---
    started = []
    skipped = []
    strategies = []
    strategy_queues = {}
    alpha_tasks: dict[str, list[asyncio.Task]] = {}
    owned_leases = []
    stop_event = asyncio.Event()
    metrics_server = None
    renew_task = None
    pubsub_task = None
    strategy_tasks = []
    try:
        for alpha_cfg in claimed_configs:
            alpha_id = alpha_cfg["alpha_id"]
            if lease is not None and not lease.is_valid(alpha_id):
                logger.warning(
                    "[RUNNER] Lease not owned by us after claim, skipping alpha=%s",
                    alpha_id,
                    extra={"alpha_id": alpha_id},
                )
                skipped.append(alpha_id)
                continue
            if lease is not None:
                owned_leases.append(alpha_id)
            ctx = StrategyContext(
                alpha_id=alpha_id,
                version=alpha_cfg.get("version", "1"),
                cache=cache,
                signal_dispatcher=dispatcher,
                state=StrategyRuntimeState(lease_valid=True),
                warmup_min_symbol_coverage=cfg.warmup_min_symbol_coverage,
                redis_client=redis_client,
                mds_redis_client=mds_client,
                panel_feature_cache=panel_feature_cache,
                price_alerts=PriceAlertProxy(
                    symbols=set(),
                    alpha_id=alpha_id,
                    exchange=cfg.mds_exchange,
                    mds_client=mds_client,
                ),
            )
            strategy = registry.create(
                alpha_cfg["strategy"],
                alpha_id,
                alpha_cfg.get("version", "1"),
                alpha_cfg.get("params", {}),
                ctx,
            )
            strategies.append(strategy)
            started.append(alpha_id)
            logger.info(
                "[RUNNER] Strategy initialized alpha=%s strategy=%s version=%s",
                alpha_id,
                alpha_cfg["strategy"],
                alpha_cfg.get("version", "1"),
                extra={"alpha_id": alpha_id},
            )
            if pubsub is not None:
                for channel in _translate_channels(
                    strategy.get_required_channels_instance(), cfg.mds_exchange
                ):
                    strategy_queues[alpha_id] = await pubsub.subscribe(
                        channel, alpha_id
                    )

        logger.info(
            "[RUNNER] Strategies initialized started=%d skipped=%d skipped_ids=%s",
            len(started),
            len(skipped),
            ",".join(skipped) if skipped else "-",
        )

        warmup = WarmupManager(
            cache,
            warmup_backend,
            snapshot_reader=snapshot_reader,
            exchange=cfg.mds_exchange,
            metrics=metrics,
            max_concurrent_mds_requests=cfg.warmup.max_concurrent_mds_requests,
            max_mds_requests_per_minute=cfg.warmup.max_mds_requests_per_minute,
            max_symbols_per_mds_request=cfg.warmup.max_symbols_per_mds_request,
            request_timeout_sec=cfg.warmup.request_timeout_sec,
            response_cache_ttl_sec=cfg.warmup.response_cache_ttl_sec,
            skip_gap_check=(cfg.mds_exchange == "tcbs"),
        )
        requirements = warmup.collect_requirements(strategies)
        logger.info(
            "[RUNNER-WARMUP] Requirements collected keys=%d total_symbols=%d max_bars=%d",
            len(requirements),
            len({symbol for symbol, _tf in requirements}),
            max(requirements.values(), default=0),
        )

        # --- Parquet cache restore ---
        parquet_restored = 0
        if not dry_run and cfg.runner_cache_dir:
            from runner.data_layer.parquet_restore import restore_from_parquet

            claimed_tfs = set()
            for c in claimed_configs:
                claimed_tfs.update(c.get("tf_set", []))
            requirement_tfs = {tf for _symbol, tf in requirements}
            tfs_to_rollup = sorted(requirement_tfs - {"1m"})
            restore_symbols, tail_rows_by_symbol, restore_requirements = (
                _parquet_restore_plan(requirements)
            )
            needs_1m = "1m" in requirement_tfs
            parquet_restored = restore_from_parquet(
                cfg.runner_cache_dir,
                cfg.mds_exchange,
                cache,
                tfs_to_rollup=tfs_to_rollup if tfs_to_rollup else None,
                symbols=restore_symbols,
                tail_rows_by_symbol=tail_rows_by_symbol,
                clear_unrequired_1m_after_rollup=bool(tfs_to_rollup) and not needs_1m,
                requirements=restore_requirements,
            )
            if parquet_restored > 0:
                logger.info(
                    "[RUNNER] Parquet restored %d candles — MDS warmup will fill delta only",
                    parquet_restored,
                )
        elif not dry_run and not cfg.runner_cache_dir:
            logger.info(
                "[RUNNER] No parquet cache dir configured, skipping parquet restore"
            )

        if keep_running and not dry_run and lease is not None:
            renew_task = asyncio.create_task(
                renew_strategy_leases(
                    lease, strategies, cfg.lease_renew_interval_sec, stop_event
                )
            )
        if not dry_run:
            from runner.data_layer.mds_ready import MDSReadyWatcher

            ready_watcher = MDSReadyWatcher(mds_client, cfg.mds_exchange)
            logger.info("[RUNNER-WARMUP] Starting synced warmup")
            max_staleness = None
            if parquet_restored > 0:
                max_staleness = float(cfg.warmup.parquet_max_staleness_sec)
            warmup_ok = await warmup.run_synced_warmup(
                ready_watcher=ready_watcher,
                mds_ready_timeout_sec=cfg.warmup.mds_ready_timeout_sec,
                min_warmup_coverage_pct=cfg.warmup.min_warmup_coverage_pct,
                sync_tolerance_candles=cfg.warmup.sync_tolerance_candles,
                max_staleness_sec=max_staleness,
            )
            if not warmup_ok:
                logger.warning(
                    "[RUNNER-WARMUP] Synced warmup incomplete — some strategies may start STALE"
                )
        for strategy in strategies:
            strategy.ctx.state.ready = warmup.strategy_ready(
                strategy, cfg.warmup_min_symbol_coverage
            )
            strategy.ctx.excluded_symbols = warmup.excluded_symbols
        if pubsub is not None:
            pubsub.set_reconnect_handler(
                warmup,
                cfg.warmup.reconnect_staleness_candles,
                trading_session=cfg.trading_session,
            )
            pubsub._metrics = metrics
        ready_count = sum(1 for strategy in strategies if strategy.ctx.state.ready)
        logger.info(
            "[RUNNER-WARMUP] Initial warmup complete ready=%d/%d cache_keys=%d",
            ready_count,
            len(strategies),
            len(cache.stats()),
        )
        for strategy in strategies:
            if not strategy.ctx.state.ready:
                continue
            try:
                bundle = await strategy._shared_panel_bundle()
                if bundle is not None:
                    strategy._last_processed_candle = bundle.latest
                if hasattr(strategy, "_warmup_complete"):
                    strategy._warmup_complete = True
                logger.info(
                    "[RUNNER] Warmup synced to candle=%s (one bar back) alpha=%s — waiting for next bar",
                    getattr(strategy, "_last_processed_candle", 0),
                    strategy.alpha_id,
                    extra={"alpha_id": strategy.alpha_id},
                )
            except Exception:
                logger.exception(
                    "[RUNNER] Initial scan failed alpha=%s",
                    strategy.alpha_id,
                    extra={"alpha_id": strategy.alpha_id},
                )

        result = {
            "runner_id": cfg.runner_id,
            "started": started,
            "skipped": skipped,
            "requirements": {
                f"{symbol}:{tf}": bars
                for (symbol, tf), bars in sorted(requirements.items())
            },
            "cache": {
                f"{row.symbol}:{row.tf}": {
                    "loaded_bars": row.loaded_bars,
                    "warmup_bars": row.warmup_bars,
                    "retain_bars": row.retain_bars,
                    "trim_count": row.trim_count,
                }
                for row in cache.stats()
            },
            "dry_run": dry_run,
        }
        if keep_running and not dry_run:
            install_stop_signal_handlers(stop_event)
            metrics_server = MetricsServer(
                lambda: runner_metrics_snapshot(
                    metrics, cfg, strategies, lease, cache, panel_feature_cache
                ),
                host=metrics_host,
                port=metrics_port,
            )
            await metrics_server.start()
            logger.info(
                "[RUNNER] Metrics server ready on %s:%s", metrics_host, metrics_port
            )
            pubsub_task = asyncio.create_task(pubsub.run(stop_event))
            for strategy in strategies:
                queue = strategy_queues.get(strategy.ctx.alpha_id)
                if queue is None:
                    continue
                aid = strategy.ctx.alpha_id
                alpha_tasks.setdefault(aid, [])
                evt_task = asyncio.create_task(
                    run_strategy_event_loop(
                        strategy, queue, stop_event, metrics, scan_semaphore
                    )
                )
                mng_task = asyncio.create_task(
                    run_strategy_manage_loop(strategy, stop_event)
                )
                strategy_tasks.extend([evt_task, mng_task])
                alpha_tasks[aid].extend([evt_task, mng_task])
            strategy_tasks.append(
                asyncio.create_task(
                    run_price_alert_reconcile_loop(strategies, pubsub, stop_event)
                )
            )
            strategy_tasks.append(
                asyncio.create_task(
                    periodic_gap_check(cache, strategies, stop_event=stop_event)
                )
            )

            # Periodic claim task
            from runner.periodic_claim import run_periodic_claim

            async def _on_new_alphas(new_configs):
                new_configs = [
                    c for c in new_configs if c["alpha_id"] not in owned_leases
                ]
                if not new_configs:
                    return
                logger.info(
                    "[RUNNER] Periodic claim found %d new alphas", len(new_configs)
                )
                for alpha_cfg in new_configs:
                    alpha_id = alpha_cfg["alpha_id"]
                    ctx = StrategyContext(
                        alpha_id=alpha_id,
                        version=alpha_cfg.get("version", "1"),
                        cache=cache,
                        signal_dispatcher=dispatcher,
                        state=StrategyRuntimeState(lease_valid=True),
                        warmup_min_symbol_coverage=cfg.warmup_min_symbol_coverage,
                        redis_client=redis_client,
                        mds_redis_client=mds_client,
                        panel_feature_cache=panel_feature_cache,
                        price_alerts=PriceAlertProxy(
                            symbols=set(),
                            alpha_id=alpha_id,
                            exchange=cfg.mds_exchange,
                            mds_client=mds_client,
                        ),
                    )
                    strategy = registry.create(
                        alpha_cfg["strategy"],
                        alpha_id,
                        alpha_cfg.get("version", "1"),
                        alpha_cfg.get("params", {}),
                        ctx,
                    )
                    strategies.append(strategy)
                    started.append(alpha_id)
                    owned_leases.append(alpha_id)
                    # A freshly claimed/enabled alpha must not inherit a stale
                    # last-event timestamp from before it was disabled — /health
                    # would otherwise 503 the whole runner as "stale" even
                    # though the alpha is simply waiting for its next candle
                    # (e.g. a 1h-only alpha re-enabled right after its hourly
                    # candle closed). Reset so it is "not started" until the
                    # first event is processed.
                    metrics.reset_alpha_event(alpha_id)
                    logger.info(
                        "[RUNNER] Strategy initialized alpha=%s strategy=%s version=%s",
                        alpha_id,
                        alpha_cfg["strategy"],
                        alpha_cfg.get("version", "1"),
                        extra={"alpha_id": alpha_id},
                    )
                    alpha_tasks.setdefault(alpha_id, [])
                    q = None
                    for channel in _translate_channels(
                        strategy.get_required_channels_instance(), cfg.mds_exchange
                    ):
                        q = await pubsub.subscribe(channel, alpha_id)
                    if q is not None:
                        evt_task = asyncio.create_task(
                            run_strategy_event_loop(
                                strategy, q, stop_event, metrics, scan_semaphore
                            )
                        )
                        mng_task = asyncio.create_task(
                            run_strategy_manage_loop(strategy, stop_event)
                        )
                        strategy_tasks.extend([evt_task, mng_task])
                        alpha_tasks[alpha_id].extend([evt_task, mng_task])
                    refresh_strategy_readiness(strategy)
                logger.info(
                    "[RUNNER] New alphas started: %s",
                    [c["alpha_id"] for c in new_configs],
                )

            strategy_tasks.append(
                asyncio.create_task(
                    run_periodic_claim(
                        redis_client,
                        cfg.runner_id,
                        cfg.max_alphas_per_runner,
                        cfg.lease_ttl_sec,
                        cfg.claim_interval_sec,
                        stop_event,
                        on_new_alphas=_on_new_alphas,
                        retry_delay_sec=cfg.claim_retry_delay_sec,
                        currently_owned_fn=lambda: list(owned_leases),
                        allowed_alpha_ids=allowed_alpha_ids,
                    )
                )
            )

            # Config listener task
            from runner.config_listener import run_config_listener

            async def _on_disabled(disabled_ids):
                logger.info("[RUNNER] Config update disabled alphas: %s", disabled_ids)
                for alpha_id in disabled_ids:
                    logger.info(
                        "[RUNNER] Strategy disabled alpha=%s",
                        alpha_id,
                        extra={"alpha_id": alpha_id},
                    )
                    if lease is not None:
                        lease.release(alpha_id)
                        if alpha_id in owned_leases:
                            owned_leases.remove(alpha_id)
                    strategy_to_remove = None
                    for s in strategies:
                        if s.alpha_id == alpha_id:
                            strategy_to_remove = s
                            break
                    if strategy_to_remove is not None:
                        strategies.remove(strategy_to_remove)
                        strategy_to_remove.ctx.state.lease_valid = False
                        if pubsub is not None:
                            await pubsub.unsubscribe_strategy(alpha_id)
                        strategy_queues.pop(alpha_id, None)
                        for t in alpha_tasks.pop(alpha_id, []):
                            if not t.done():
                                t.cancel()

            strategy_tasks.append(
                asyncio.create_task(
                    run_config_listener(
                        redis_client,
                        cfg.runner_id,
                        currently_owned_fn=lambda: list(owned_leases),
                        on_disabled=_on_disabled,
                        on_new_alphas=_on_new_alphas,
                        stop_event=stop_event,
                        max_alphas_per_runner=cfg.max_alphas_per_runner,
                        ttl_sec=cfg.lease_ttl_sec,
                        allowed_alpha_ids=allowed_alpha_ids,
                    )
                )
            )
            logger.info("[RUNNER] Event loops started tasks=%d", len(strategy_tasks))
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                raise
        if dry_run:
            print(result)
        return result
    finally:
        stop_event.set()
        if renew_task is not None:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
        if metrics_server is not None:
            await metrics_server.stop()
        for task in [pubsub_task, *strategy_tasks]:
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *[task for task in [pubsub_task, *strategy_tasks] if task is not None],
            return_exceptions=True,
        )
        if pubsub is not None:
            for strategy in strategies:
                await pubsub.unsubscribe_strategy(strategy.ctx.alpha_id)
        if lease is not None:
            for alpha_id in owned_leases:
                lease.release(alpha_id)
        if compute_executor is not None:
            compute_executor.shutdown(wait=False, cancel_futures=True)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.getenv("RUNNER_CONFIG"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--metrics-host", default=os.getenv("RUNNER_METRICS_HOST", "0.0.0.0")
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.getenv("RUNNER_METRICS_PORT", "9091")),
    )
    args = parser.parse_args()
    if not args.config:
        parser.error("--config or RUNNER_CONFIG is required")
    asyncio.run(
        run(
            args.config,
            dry_run=args.dry_run,
            keep_running=not args.dry_run,
            metrics_host=args.metrics_host,
            metrics_port=args.metrics_port,
        )
    )


if __name__ == "__main__":
    main()
