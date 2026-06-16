from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import signal

import redis

from runner.config import load_runner_config
from runner.data_layer.cache import SharedCandleCache
from runner.data_layer.pubsub import DataEvent, SharedPubSubManager
from runner.data_layer.snapshot import SnapshotReader
from runner.data_layer.warmup import MDSWarmupBackend, WarmupManager
from runner.lease import LeaseManager
from runner.metrics import RunnerMetrics
from runner.metrics_http import MetricsServer
from runner.reconcile.state import StrategyRuntimeState
from runner.signal.dispatcher import SignalDispatcher
from runner.strategy.context import StrategyContext
from runner.strategy.registry import StrategyRegistry


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


def runner_metrics_snapshot(metrics: RunnerMetrics, cfg, strategies, lease, cache: SharedCandleCache | None = None) -> dict:
    snapshot = metrics.snapshot()
    snapshot.update({
        "runner_id": cfg.runner_id,
        "signal_stream": cfg.signal_stream,
        "shadow_mode": cfg.shadow_mode,
        "strategies_active": sum(1 for s in strategies if s.ctx.state.ready and s.ctx.state.lease_valid),
        "strategies_suspended": sum(1 for s in strategies if not s.ctx.state.ready or not s.ctx.state.lease_valid),
        "lease_owner": {},
    })
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
            if not strategy.ctx.state.lease_valid:
                continue
            try:
                ok = lease.renew(strategy.ctx.alpha_id)
            except Exception:
                ok = False
            if not ok:
                strategy.ctx.state.lease_valid = False
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
        ready = strategy.ctx.update_readiness(symbols, tf, bars, max_age_sec=max_age_sec) and ready
    strategy.ctx.state.ready = ready


async def handle_strategy_event(strategy, event: DataEvent) -> None:
    if event.kind == "kline" and event.symbol and event.tf:
        refresh_strategy_readiness(strategy)
        await strategy.on_candle(event.symbol, event.tf)
        await strategy.scan()
        return

    if event.kind == "price_alert" and event.symbol:
        payload = event.payload or {}
        price = float(payload.get("price", payload.get("bid", payload.get("ask", 0.0))) or 0.0)
        side = str(payload.get("side", ""))
        await strategy.on_price_alert(event.symbol, price, side)
        await strategy.scan()
        return

    if event.kind == "symbols":
        await strategy.scan()


async def run_strategy_event_loop(strategy, queue: asyncio.Queue, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        try:
            await handle_strategy_event(strategy, event)
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
    cfg = load_runner_config(config_path)
    registry = build_registry(cfg.modules)
    cache = SharedCandleCache(data_max_candles_floor=cfg.cache.min_retain_bars)
    metrics = RunnerMetrics()

    if dry_run:
        lease = None
        dispatcher = None
        pubsub = None
        warmup_backend = _noop_backend
        snapshot_reader = None
    else:
        redis_client = redis.from_url(cfg.redis_url, decode_responses=True)
        mds_client = redis.from_url(cfg.mds_redis_url or cfg.redis_url, decode_responses=True)
        lease = LeaseManager(redis_client, cfg.runner_id, cfg.lease_ttl_sec)
        dispatcher = SignalDispatcher(redis_client, cfg.signal_stream, lease)
        pubsub = SharedPubSubManager(mds_client, cache, cfg.data_queue_maxsize)
        warmup_backend = MDSWarmupBackend(
            mds_client,
            cfg.mds_exchange,
            cfg.runner_id,
            cfg.warmup.request_timeout_sec,
        )
        snapshot_reader = SnapshotReader(mds_client, cfg.mds_exchange)

    started = []
    skipped = []
    strategies = []
    strategy_queues = {}
    owned_leases = []
    stop_event = asyncio.Event()
    metrics_server = None
    renew_task = None
    pubsub_task = None
    strategy_tasks = []
    try:
        for alpha in cfg.alphas:
            if lease is not None and not lease.acquire(alpha.alpha_id):
                skipped.append(alpha.alpha_id)
                continue
            if lease is not None:
                owned_leases.append(alpha.alpha_id)
            ctx = StrategyContext(
                alpha_id=alpha.alpha_id,
                version=alpha.version,
                cache=cache,
                signal_dispatcher=dispatcher,
                state=StrategyRuntimeState(lease_valid=True),
                warmup_min_symbol_coverage=cfg.warmup_min_symbol_coverage,
            )
            strategy = registry.create(alpha.strategy, alpha.alpha_id, alpha.version, alpha.params, ctx)
            strategies.append(strategy)
            started.append(alpha.alpha_id)
            if pubsub is not None:
                for channel in strategy.get_required_channels():
                    strategy_queues[alpha.alpha_id] = await pubsub.subscribe(channel, alpha.alpha_id)

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
        )
        requirements = warmup.collect_requirements(strategies)
        if not dry_run:
            await warmup.request_warmup(requirements)
        for strategy in strategies:
            strategy.ctx.state.ready = warmup.strategy_ready(strategy, cfg.warmup_min_symbol_coverage)

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
                lambda: runner_metrics_snapshot(metrics, cfg, strategies, lease, cache),
                host=metrics_host,
                port=metrics_port,
            )
            await metrics_server.start()
            renew_task = asyncio.create_task(
                renew_strategy_leases(lease, strategies, cfg.lease_renew_interval_sec, stop_event)
            )
            pubsub_task = asyncio.create_task(pubsub.run(stop_event))
            for strategy in strategies:
                queue = strategy_queues.get(strategy.ctx.alpha_id)
                if queue is None:
                    continue
                strategy_tasks.append(asyncio.create_task(run_strategy_event_loop(strategy, queue, stop_event)))
                strategy_tasks.append(asyncio.create_task(run_strategy_manage_loop(strategy, stop_event)))
            try:
                await renew_task
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.getenv("RUNNER_CONFIG"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--metrics-host", default=os.getenv("RUNNER_METRICS_HOST", "0.0.0.0"))
    parser.add_argument("--metrics-port", type=int, default=int(os.getenv("RUNNER_METRICS_PORT", "9091")))
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
