import asyncio
import json
import logging
import os
import time

import redis.asyncio as redis_lib

from app.config import settings
from app.db import Database
from app.executor import Executor
from app.logging_config import configure_structured_logging
from app.metrics import WorkerMetrics
from app.models import SignalType, parse_signal
from app.ob_exec import (
    ObExecCache,
    PriceQuote,
    make_exit_price_fn,
    run_ob_exec_subscriber,
)
from app.ob_subscribe import (
    publish_empty_syncs,
    publish_subscribe,
    run_orderbook_sync_loop,
)
from app.redis_clients import (
    connect_mds_redis,
    connect_paper_redis,
    make_mds_redis_client,
)
from app.fill import fixed_pct_fill
from app.slippage_client import (
    SlippageClient,
    FillService,
    FillResolution,
    order_side_for,
)
from app.position_snapshots import PositionSnapshotPublisher
from app.position_ownership import PositionOwnershipMonitor
from app.equity_snapshots import EquitySnapshotCollector
from app.virtual_trade_ledger import run_virtual_trade_consumer

logger = logging.getLogger(__name__)


def install_uvloop_if_available() -> None:
    try:
        import uvloop
    except ImportError:
        return
    uvloop.install()


class TickerPriceCache:
    def __init__(self, staleness_sec: float = 5.0, clock=time.monotonic):
        self._prices: dict[str, tuple[float, float]] = {}
        self._staleness_sec = staleness_sec
        self._clock = clock

    def update_price(self, symbol: str, price: float) -> None:
        if price > 0:
            self._prices[symbol] = (price, self._clock())

    def get_prices(self, symbols: list[str] | None = None) -> dict[str, float]:
        selected = symbols if symbols is not None else list(self._prices)
        return {
            symbol: quote.price
            for symbol in selected
            if (quote := self.get_quote(symbol)) is not None
        }

    def get_price(self, symbol: str) -> float | None:
        quote = self.get_quote(symbol)
        return quote.price if quote is not None else None

    def get_quote(self, symbol: str) -> PriceQuote | None:
        item = self._prices.get(symbol)
        if item is None:
            return None
        price, ts = item
        if self._clock() - ts > self._staleness_sec:
            return None
        return PriceQuote(price=price, source="ticker_mid", is_executable=False)


class TickFillService:
    def __init__(self, cache: TickerPriceCache, slippage_pct: float) -> None:
        self._cache = cache
        self._slippage_pct = slippage_pct

    async def resolve(
        self,
        exchange: str,
        symbol: str,
        position_side: str,
        qty: float,
        ref_price: float,
        is_close: bool,
        request_id: str | None = None,
        ref_is_executable: bool = False,
    ) -> FillResolution:
        order_side = order_side_for(position_side, is_close)
        quote = self._cache.get_quote(symbol)
        if quote is not None:
            return FillResolution(
                final_price=quote.price,
                order_side=order_side,
                initial_price=quote.price,
                initial_source=quote.source,
                requested_qty=qty,
                filled_qty=qty,
            )

        fallback_price = (
            ref_price
            if ref_is_executable
            else fixed_pct_fill(ref_price, position_side, self._slippage_pct, is_close)
        )
        return FillResolution(
            final_price=fallback_price,
            order_side=order_side,
            initial_price=fallback_price,
            initial_source="executable_ref" if ref_is_executable else "fixed_pct",
            requested_qty=qty,
            filled_qty=0.0,
            fallback_reason="ticker_unavailable",
        )


def close_ref_is_executable(signal) -> bool:
    return Executor.close_ref_is_executable(signal)


def configure_logging() -> None:
    configure_structured_logging(
        service_name="paper-trade",
        log_level=settings.LOG_LEVEL,
    )


async def process_signal_message(
    data: dict,
    db: Database,
    executor: Executor,
    fill_service=None,
    snapshot_publisher=None,
    pre_open=None,
    metrics=None,
) -> dict | None:
    signal_id = data.get("signal_id", "unknown")
    alpha_id = data.get("alpha_id", "unknown")
    signal_type = data.get("type", "unknown")

    if metrics is not None:
        metrics.inc("received_total")

    if signal_id != "unknown" and await db.signal_processed(signal_id):
        if metrics is not None:
            metrics.inc("duplicate_skipped_total")
        logger.warning("Duplicate signal %s skipped before processing", signal_id)
        return None

    try:
        signal = parse_signal(data)
    except Exception as exc:
        if metrics is not None:
            metrics.inc_parse_error(alpha_id)
        logger.error("Error parsing signal %s: %s", signal_id, exc)
        async with db.transaction():
            await db.log_signal(
                signal_id=signal_id,
                alpha_id=alpha_id,
                signal_type=signal_type,
                payload=json.dumps(data),
            )
            await db.mark_signal_processed(signal_id, error=str(exc))
        return None

    # Resolve the book-walked fill price OUTSIDE the DB transaction (spec §8.2): an RPC
    # BLPOP must never be held inside the SQLite writer lock.
    fill_price = None
    if fill_service is not None:
        try:
            if signal.type == SignalType.OPEN:
                # Alpha 4 and Alpha 10 intentionally use multiple independently
                # managed legs.  Resolve a real fill for every OPEN; the executor
                # retains the global duplicate guard unless the signal opts in.
                pre_subscribe_outcome = (
                    await pre_open(signal) if pre_open is not None else None
                )
                fill_price = await fill_service.resolve(
                    signal.exchange,
                    signal.symbol,
                    signal.side,
                    signal.qty,
                    ref_price=signal.entry,
                    is_close=False,
                )
                if pre_subscribe_outcome and hasattr(fill_price, "metadata"):
                    fill_price.pre_subscribe_outcome = pre_subscribe_outcome
            elif signal.type == SignalType.CLOSE:
                pos = await db.get_position(signal.position_id)
                if pos:
                    raw_exit = Executor.close_ref_price(signal, pos)
                    qty = (
                        signal.qty
                        if (signal.qty is not None and signal.qty > 0)
                        else pos["qty"]
                    )
                    fill_price = await fill_service.resolve(
                        pos.get("exchange", "binance"),
                        pos["symbol"],
                        pos["side"],
                        qty,
                        ref_price=raw_exit,
                        is_close=True,
                        ref_is_executable=close_ref_is_executable(signal),
                    )
        except Exception as exc:
            logger.warning("Fill resolve failed for %s: %s", signal_id, exc)
            fill_price = None  # executor falls back to fixed-pct

    async with db.transaction():
        await db.log_signal(
            signal_id=signal_id,
            alpha_id=alpha_id,
            signal_type=signal_type,
            payload=json.dumps(data),
        )
        try:
            if signal.type == SignalType.OPEN:
                result = await executor.process_open(signal, fill_price=fill_price)
            elif signal.type == SignalType.MODIFY:
                result = await executor.process_modify(signal)
            elif signal.type == SignalType.CLOSE:
                result = await executor.process_close(signal, fill_price=fill_price)
            elif signal.type == SignalType.REGISTER_COLUMNS:
                result = await executor.process_register_columns(signal)
            else:
                result = None
            await db.mark_signal_processed(signal_id)
            committed_result = result
            if metrics is not None:
                metrics.inc_committed(signal_type)
        except Exception as exc:
            if metrics is not None:
                metrics.inc_process_error(alpha_id)
            logger.error("Error processing signal %s: %s", signal_id, exc)
            await db.mark_signal_processed(signal_id, error=str(exc))
            return None
    if (
        snapshot_publisher is not None
        and committed_result is not None
        and signal.type in {SignalType.OPEN, SignalType.MODIFY, SignalType.CLOSE}
    ):
        await snapshot_publisher.publish_after_commit(signal.alpha_id)
    return committed_result


async def handle_signal_message(
    msg_id: str,
    data: dict,
    *,
    db: Database,
    executor: Executor,
    fill_service,
    snapshot_publisher,
    orderbook_enabled: bool,
    supported_exchanges: set[str],
    mds_redis,
    ob_cache,
    paper_redis,
    worker_metrics: WorkerMetrics,
) -> None:
    """Process one stream message and ack it -- never raises.

    2026-07-27 incident: a burst of simultaneous rebalances pushed SQLite past
    its busy_timeout ("database is locked"). That error originated in
    ``db.transaction()``'s ``BEGIN IMMEDIATE`` -- outside the try/except inside
    ``process_signal_message`` -- so it propagated out of the consumer loop and
    crashed the whole worker process. Every message already delivered by that
    XREADGROUP batch was then stuck un-acked in the PEL forever (CONSUMER_NAME
    is fixed, and the loop only reads new ">" entries on restart). A single
    alpha's rebalance (e.g. 1h-blend-close) silently never applied.
    Catching here keeps one message's failure from taking down every other
    alpha's processing; the message stays pending for deliberate XCLAIM-based
    review instead of vanishing into a crash-restart loop.
    """
    alpha_id = data.get("alpha_id", "unknown")
    signal_id = data.get("signal_id", "unknown")
    try:

        async def pre_open(signal):
            exchange = str(signal.exchange).lower()
            if (
                not settings.OPEN_BOOK_PRE_SUBSCRIBE_ENABLED
                or exchange not in supported_exchanges
            ):
                return "unsupported_exchange"
            try:
                await publish_subscribe(
                    mds_redis, exchange, settings.CONSUMER_NAME, signal.symbol
                )
            except Exception:
                logger.exception(
                    "[OPEN-PRE-SUBSCRIBE] publish failed symbol=%s", signal.symbol
                )
                return "pre_subscribe_publish_failed"
            return await ob_cache.wait_ready(
                exchange, signal.symbol, settings.OPEN_BOOK_READY_TIMEOUT_MS / 1000.0
            )

        result = await process_signal_message(
            data,
            db,
            executor,
            fill_service=fill_service,
            snapshot_publisher=snapshot_publisher,
            pre_open=pre_open if orderbook_enabled else None,
            metrics=worker_metrics,
        )
        if result is not None:
            logger.info("Processed %s signal: %s", data.get("type"), result)
        exchange = str(data.get("exchange", "binance")).lower()
        if (
            result is not None
            and data.get("type") == "OPEN"
            and orderbook_enabled
            and exchange in supported_exchanges
        ):
            try:
                # Bounded so a stalled publish can't freeze the consumer loop.
                await asyncio.wait_for(
                    publish_subscribe(
                        mds_redis,
                        exchange,
                        settings.CONSUMER_NAME,
                        data.get("symbol", ""),
                    ),
                    timeout=2.0,
                )
            except Exception as exc:
                logger.warning("orderbook subscribe publish failed: %s", exc)
        await paper_redis.xack(
            settings.REDIS_STREAM,
            settings.CONSUMER_GROUP,
            msg_id,
        )
        worker_metrics.inc("xack_total")
    except Exception:
        worker_metrics.inc_left_pending(alpha_id)
        logger.critical(
            "[CONSUMER] Unhandled error processing msg_id=%s signal_id=%s alpha_id=%s; "
            "leaving message un-acked in the PEL instead of crashing (see "
            "docs/REBALANCE_RECOVERY.md for manual recovery)",
            msg_id,
            signal_id,
            alpha_id,
            exc_info=True,
        )


async def run_ticker_subscriber(
    cache: TickerPriceCache, connect_redis, exchanges: set[str]
) -> None:
    channels = [f"ticker:{exchange}" for exchange in sorted(exchanges)]
    while True:
        redis_client = None
        pubsub = None
        try:
            redis_client = await connect_redis()
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(*channels)
            logger.info("[TICKER] Subscribed to MDS channels: %s", channels)
            while True:
                msg = await pubsub.get_message(timeout=1.0)
                if not msg or msg["type"] != "message":
                    continue

                data = json.loads(msg["data"])
                symbol = data.get("symbol", "")
                price = data.get("price")
                if symbol and price is not None:
                    cache.update_price(symbol, float(price))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Ticker subscriber error: %s", exc)
            await asyncio.sleep(5)
        finally:
            if pubsub is not None:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            if redis_client is not None:
                await redis_client.aclose()


async def run_price_alert_bridge(
    connect_mds, connect_paper, exchanges: set[str]
) -> None:
    ticker_channels = [f"ticker:{exchange}" for exchange in sorted(exchanges)]
    alert_patterns = [f"price_alert:{exchange}:*" for exchange in sorted(exchanges)]
    while True:
        mds_client = None
        paper_client = None
        pubsub = None
        try:
            mds_client = await connect_mds()
            paper_client = await connect_paper()
            pubsub = mds_client.pubsub()
            await pubsub.subscribe(*ticker_channels)
            await pubsub.psubscribe(*alert_patterns)
            logger.info(
                "[PRICE-ALERT-BRIDGE] Bridging %s + %s → paper-redis price_alert",
                ticker_channels,
                alert_patterns,
            )
            while True:
                msg = await pubsub.get_message(timeout=1.0)
                if not msg or msg["type"] not in ("message", "pmessage"):
                    continue
                await paper_client.publish("price_alert", msg["data"])
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("[PRICE-ALERT-BRIDGE] error: %s", exc)
            await asyncio.sleep(5)
        finally:
            if pubsub is not None:
                await pubsub.punsubscribe()
                await pubsub.unsubscribe()
                await pubsub.aclose()
            if mds_client is not None:
                await mds_client.aclose()
            if paper_client is not None:
                await paper_client.aclose()


async def run_price_check_loop(
    db: Database,
    executor: Executor,
    cache: TickerPriceCache,
    ob_cache: ObExecCache,
    fill_service,
) -> None:
    exit_price_fn = make_exit_price_fn(ob_cache, cache)

    fill_resolver = None
    if fill_service is not None:

        async def fill_resolver(
            exchange,
            symbol,
            position_side,
            qty,
            ref_price,
            is_close,
            ref_is_executable=False,
        ):
            return await fill_service.resolve(
                exchange,
                symbol,
                position_side,
                qty,
                ref_price,
                is_close,
                ref_is_executable=ref_is_executable,
            )

    while True:
        try:
            await asyncio.sleep(settings.PRICE_CHECK_INTERVAL)
            symbols = await db.get_symbols_with_open_positions()
            if not symbols:
                continue

            hits = await executor.check_tpsl_hits(
                exit_price_fn, fill_resolver=fill_resolver
            )
            for hit in hits:
                logger.info("[TPSL] Auto-closed: %s", hit)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Price check error: %s", exc, exc_info=True)
            await asyncio.sleep(5)


async def run_health_loop(ownership_monitor=None) -> None:
    while True:
        try:
            with open("/tmp/bot_health", "w") as health_file:
                health_file.write(
                    json.dumps(
                        {
                            "timestamp": time.time(),
                            "ownership": ownership_monitor.last_report
                            if ownership_monitor
                            else {},
                        }
                    )
                )
        except Exception:
            logger.warning("Failed to write worker health file", exc_info=True)
        await asyncio.sleep(10)


async def run_reconcile_log_loop(metrics, interval_sec: float) -> None:
    """Periodically emit a structured reconciliation snapshot so signal drops
    are observable in logs ("no silent failures"). Durable audit lives in
    scripts/reconcile_signals.py; this is the live process-local view."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            snapshot = metrics.snapshot()
            if snapshot["reconciles"]:
                logger.info("[RECONCILE] %s", json.dumps(snapshot))
            else:
                logger.error(
                    "[RECONCILE] worker counter invariant broken: %s",
                    json.dumps(snapshot),
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("[RECONCILE] snapshot failed", exc_info=True)


async def register_configured_alphas(db: Database) -> None:
    alpha_ids = [
        alpha_id.strip()
        for alpha_id in settings.REGISTERED_ALPHAS.split(",")
        if alpha_id.strip()
    ]
    for alpha_id in alpha_ids:
        await db.register_alpha(alpha_id)
        logger.info("Registered alpha from config: %s", alpha_id)


async def ensure_consumer_group(redis_client) -> None:
    try:
        await redis_client.xgroup_create(
            settings.REDIS_STREAM, settings.CONSUMER_GROUP, id="0", mkstream=True
        )
    except redis_lib.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def run_consumer():
    configure_logging()
    settings.validate_runtime()
    try:
        os.remove("/tmp/bot_health")
    except FileNotFoundError:
        pass

    db = Database(settings.DB_PATH)
    await db.init()
    await register_configured_alphas(db)
    pruned = await db.prune_signals(settings.SIGNAL_RETENTION_DAYS)
    if pruned:
        logger.info("Pruned %d old signals", pruned)

    executor = Executor(
        db,
        slippage_pct=settings.SLIPPAGE_PCT,
        duplicate_policy=settings.DUPLICATE_POSITION_POLICY,
    )
    orderbook_enabled = settings.orderbook_enabled()
    supported_exchanges = settings.get_orderbook_exchanges()
    cache = TickerPriceCache(staleness_sec=settings.TICKER_STALENESS_SEC)
    ob_cache = ObExecCache(staleness_sec=settings.OPEN_BOOK_MAX_AGE_MS / 1000.0)

    paper_redis = await connect_paper_redis()
    needs_tick_execution = not orderbook_enabled
    mds_redis = (
        make_mds_redis_client()
        if (
            orderbook_enabled
            or settings.ENABLE_WORKER_TPSL_AUTO_CLOSE
            or settings.ENABLE_POSITION_OWNERSHIP_MONITOR
            or needs_tick_execution
        )
        else None
    )
    fill_service = None
    if orderbook_enabled and mds_redis is not None:
        fill_service = FillService(
            SlippageClient(mds_redis),
            slippage_pct=settings.SLIPPAGE_PCT,
            timeout=settings.SLIPPAGE_RPC_TIMEOUT,
            supported_exchanges=supported_exchanges,
            latency_model_enabled=settings.EXECUTION_LATENCY_MODEL_ENABLED,
            latency_ms=settings.EXECUTION_LATENCY_MS,
            min_adverse_bps=settings.EXECUTION_MIN_ADVERSE_BPS,
            second_quote_timeout=settings.EXECUTION_SECOND_QUOTE_TIMEOUT_MS / 1000.0,
        )
    elif needs_tick_execution:
        fill_service = TickFillService(cache, settings.SLIPPAGE_PCT)
    snapshot_publisher = PositionSnapshotPublisher(
        db, paper_redis, settings.POSITION_SNAPSHOT_SYNC_INTERVAL_SEC
    )
    ownership_monitor = (
        PositionOwnershipMonitor(
            db,
            paper_redis,
            mds_redis,
            settings.POSITION_OWNERSHIP_GRACE_SEC,
            settings.POSITION_OWNERSHIP_CHECK_INTERVAL_SEC,
        )
        if settings.ENABLE_POSITION_OWNERSHIP_MONITOR and mds_redis is not None
        else None
    )
    ticker_task = None
    price_check_task = None
    health_task = None
    ob_exec_tasks = []
    ob_sync_task = None
    snapshot_task = None
    ownership_task = None
    equity_snapshot_collector = None
    equity_snapshot_task = None
    price_alert_bridge_task = None
    reconcile_task = None
    virtual_trade_task = None
    worker_metrics = WorkerMetrics()
    if settings.ENABLE_EQUITY_SNAPSHOT:
        equity_snapshot_collector = EquitySnapshotCollector(
            db,
            cache,
            settings.EQUITY_SNAPSHOT_DB_PATH,
            settings.EQUITY_SNAPSHOT_INTERVAL_SEC,
            settings.ALPHAS_DIR,
            paper_redis,
        )

    try:
        await ensure_consumer_group(paper_redis)
        virtual_trade_task = asyncio.create_task(
            run_virtual_trade_consumer(
                connect_redis=connect_paper_redis,
                db_path=settings.DB_PATH,
                stream=settings.VIRTUAL_TRADE_STREAM,
                group=settings.VIRTUAL_TRADE_CONSUMER_GROUP,
                consumer=settings.VIRTUAL_TRADE_CONSUMER_NAME,
                read_count=settings.REDIS_READ_COUNT,
                block_ms=settings.REDIS_BLOCK_MS,
            )
        )

        if orderbook_enabled:
            ob_exec_tasks = [
                asyncio.create_task(
                    run_ob_exec_subscriber(ob_cache, connect_mds_redis, exchange)
                )
                for exchange in sorted(supported_exchanges)
            ]
            ob_sync_task = asyncio.create_task(
                run_orderbook_sync_loop(
                    db,
                    mds_redis,
                    settings.CONSUMER_NAME,
                    supported_exchanges,
                    settings.ORDERBOOK_SYNC_INTERVAL,
                )
            )
        await snapshot_publisher.publish_all()
        snapshot_task = asyncio.create_task(snapshot_publisher.run())
        if equity_snapshot_collector is not None:
            await equity_snapshot_collector.init()
            equity_snapshot_task = asyncio.create_task(equity_snapshot_collector.run())
        if ownership_monitor is not None:
            ownership_task = asyncio.create_task(ownership_monitor.run())

        if settings.ENABLE_WORKER_TPSL_AUTO_CLOSE or needs_tick_execution:
            ticker_task = asyncio.create_task(
                run_ticker_subscriber(cache, connect_mds_redis, supported_exchanges)
            )
        price_alert_bridge_task = asyncio.create_task(
            run_price_alert_bridge(
                connect_mds_redis, connect_paper_redis, supported_exchanges
            )
        )
        if settings.ENABLE_WORKER_TPSL_AUTO_CLOSE:
            price_check_task = asyncio.create_task(
                run_price_check_loop(db, executor, cache, ob_cache, fill_service)
            )
        else:
            logger.info(
                "Worker auto TP/SL disabled (ENABLE_WORKER_TPSL_AUTO_CLOSE=False); alphas manage SL/TP via MDS price_alert"
            )
        health_task = asyncio.create_task(run_health_loop(ownership_monitor))
        if settings.RECONCILE_LOG_INTERVAL_SEC > 0:
            reconcile_task = asyncio.create_task(
                run_reconcile_log_loop(
                    worker_metrics, settings.RECONCILE_LOG_INTERVAL_SEC
                )
            )

        logger.info(
            "Consumer started: stream=%s group=%s",
            settings.REDIS_STREAM,
            settings.CONSUMER_GROUP,
        )

        while True:
            messages = await paper_redis.xreadgroup(
                settings.CONSUMER_GROUP,
                settings.CONSUMER_NAME,
                {settings.REDIS_STREAM: ">"},
                count=settings.REDIS_READ_COUNT,
                block=settings.REDIS_BLOCK_MS,
            )

            if not messages:
                continue

            for _, msgs in messages:
                for msg_id, data in msgs:
                    await handle_signal_message(
                        msg_id,
                        data,
                        db=db,
                        executor=executor,
                        fill_service=fill_service,
                        snapshot_publisher=snapshot_publisher,
                        orderbook_enabled=orderbook_enabled,
                        supported_exchanges=supported_exchanges,
                        mds_redis=mds_redis,
                        ob_cache=ob_cache,
                        paper_redis=paper_redis,
                        worker_metrics=worker_metrics,
                    )

    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        tasks = [
            task
            for task in (
                ticker_task,
                price_check_task,
                health_task,
                ob_sync_task,
                snapshot_task,
                ownership_task,
                equity_snapshot_task,
                price_alert_bridge_task,
                reconcile_task,
                virtual_trade_task,
            )
            if task is not None
        ]
        tasks.extend(ob_exec_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if mds_redis is not None:
            try:
                if orderbook_enabled:
                    await publish_empty_syncs(
                        mds_redis, settings.CONSUMER_NAME, supported_exchanges
                    )
            except Exception as exc:
                logger.warning("orderbook empty sync failed during shutdown: %s", exc)
            await mds_redis.aclose()
        await db.close()
        if equity_snapshot_collector is not None:
            await equity_snapshot_collector.close()
        await paper_redis.aclose()


if __name__ == "__main__":
    install_uvloop_if_available()
    asyncio.run(run_consumer())
