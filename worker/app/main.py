import asyncio
import json
import logging
import os
import time

import redis.asyncio as redis_lib

from app.config import settings
from app.db import Database
from app.executor import Executor
from app.models import SignalType, parse_signal, RegisterColumnsSignal
from app.ob_exec import ObExecCache, PriceQuote, make_exit_price_fn, run_ob_exec_subscriber
from app.ob_subscribe import (
    publish_empty_syncs,
    publish_subscribe,
    run_orderbook_sync_loop,
)
from app.redis_clients import connect_mds_redis, connect_paper_redis, make_mds_redis_client
from app.slippage_client import SlippageClient, FillService
from app.position_snapshots import PositionSnapshotPublisher
from app.position_ownership import PositionOwnershipMonitor

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


def close_ref_is_executable(signal) -> bool:
    return Executor.close_ref_is_executable(signal)


def configure_logging() -> None:
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    app_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    handlers = [
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(settings.LOG_DIR, "worker.log")),
    ]
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
    # Root at WARNING to suppress third-party library noise
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.handlers = handlers
    # App loggers at configured level
    for name in ("app", "__main__", "worker"):
        logging.getLogger(name).setLevel(app_level)


async def process_signal_message(data: dict, db: Database, executor: Executor,
                                  fill_service=None, snapshot_publisher=None,
                                  pre_open=None) -> dict | None:
    signal_id = data.get("signal_id", "unknown")
    alpha_id = data.get("alpha_id", "unknown")
    signal_type = data.get("type", "unknown")

    try:
        signal = parse_signal(data)
    except Exception as exc:
        logger.error("Error parsing signal %s: %s", signal_id, exc)
        async with db.transaction():
            await db.log_signal(signal_id=signal_id, alpha_id=alpha_id,
                                signal_type=signal_type, payload=json.dumps(data))
            await db.mark_signal_processed(signal_id, error=str(exc))
        return None

    # Resolve the book-walked fill price OUTSIDE the DB transaction (spec §8.2): an RPC
    # BLPOP must never be held inside the SQLite writer lock.
    fill_price = None
    if fill_service is not None:
        try:
            if signal.type == SignalType.OPEN:
                existing = await db.get_open_position_by_alpha_symbol(
                    signal.alpha_id, signal.symbol
                )
                if existing is None:
                    pre_subscribe_outcome = await pre_open(signal) if pre_open is not None else None
                    fill_price = await fill_service.resolve(
                        signal.exchange, signal.symbol, signal.side, signal.qty,
                        ref_price=signal.entry, is_close=False,
                    )
                    if pre_subscribe_outcome and hasattr(fill_price, "metadata"):
                        fill_price.pre_subscribe_outcome = pre_subscribe_outcome
            elif signal.type == SignalType.CLOSE:
                pos = await db.get_position(signal.position_id)
                if pos:
                    raw_exit = Executor.close_ref_price(signal, pos)
                    qty = signal.qty if (signal.qty is not None and signal.qty > 0) else pos["qty"]
                    fill_price = await fill_service.resolve(
                        pos.get("exchange", "binance"), pos["symbol"], pos["side"], qty,
                        ref_price=raw_exit, is_close=True,
                        ref_is_executable=close_ref_is_executable(signal),
                    )
        except Exception as exc:
            logger.warning("Fill resolve failed for %s: %s", signal_id, exc)
            fill_price = None  # executor falls back to fixed-pct

    async with db.transaction():
        await db.log_signal(signal_id=signal_id, alpha_id=alpha_id,
                            signal_type=signal_type, payload=json.dumps(data))
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
        except Exception as exc:
            logger.error("Error processing signal %s: %s", signal_id, exc)
            await db.mark_signal_processed(signal_id, error=str(exc))
            return None
    if snapshot_publisher is not None and committed_result is not None and signal.type in {
        SignalType.OPEN, SignalType.MODIFY, SignalType.CLOSE
    }:
        await snapshot_publisher.publish_after_commit(signal.alpha_id)
    return committed_result


async def run_ticker_subscriber(cache: TickerPriceCache, connect_redis,
                                exchanges: set[str]) -> None:
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


async def run_price_check_loop(db: Database, executor: Executor, cache: TickerPriceCache,
                               ob_cache: ObExecCache, fill_service) -> None:
    exit_price_fn = make_exit_price_fn(ob_cache, cache)

    fill_resolver = None
    if fill_service is not None:
        async def fill_resolver(exchange, symbol, position_side, qty, ref_price, is_close,
                                ref_is_executable=False):
            return await fill_service.resolve(
                exchange, symbol, position_side, qty, ref_price, is_close,
                ref_is_executable=ref_is_executable,
            )
    while True:
        try:
            await asyncio.sleep(settings.PRICE_CHECK_INTERVAL)
            symbols = await db.get_symbols_with_open_positions()
            if not symbols:
                continue

            hits = await executor.check_tpsl_hits(exit_price_fn, fill_resolver=fill_resolver)
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
            healthy = ownership_monitor is None or ownership_monitor.last_report.get("healthy", False)
            if healthy:
                with open("/tmp/bot_health", "w") as health_file:
                    health_file.write(json.dumps({
                        "timestamp": time.time(),
                        "ownership": ownership_monitor.last_report if ownership_monitor else {},
                    }))
            else:
                try:
                    os.remove("/tmp/bot_health")
                except FileNotFoundError:
                    pass
        except Exception:
            logger.warning("Failed to write worker health file", exc_info=True)
        await asyncio.sleep(10)


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
    supported_exchanges = settings.get_orderbook_exchanges()
    cache = TickerPriceCache(staleness_sec=settings.TICKER_STALENESS_SEC)
    ob_cache = ObExecCache(staleness_sec=settings.OPEN_BOOK_MAX_AGE_MS / 1000.0)

    paper_redis = await connect_paper_redis()
    mds_redis = make_mds_redis_client() if (
        settings.ENABLE_ORDERBOOK_SLIPPAGE or settings.ENABLE_WORKER_TPSL_AUTO_CLOSE
        or settings.ENABLE_POSITION_OWNERSHIP_MONITOR
    ) else None
    fill_service = None
    if settings.ENABLE_ORDERBOOK_SLIPPAGE and mds_redis is not None:
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
    snapshot_publisher = PositionSnapshotPublisher(
        db, paper_redis, settings.POSITION_SNAPSHOT_SYNC_INTERVAL_SEC
    )
    ownership_monitor = PositionOwnershipMonitor(
        db, paper_redis, mds_redis,
        settings.POSITION_OWNERSHIP_GRACE_SEC,
        settings.POSITION_OWNERSHIP_CHECK_INTERVAL_SEC,
    ) if settings.ENABLE_POSITION_OWNERSHIP_MONITOR and mds_redis is not None else None
    ticker_task = None
    price_check_task = None
    health_task = None
    ob_exec_tasks = []
    ob_sync_task = None
    snapshot_task = None
    ownership_task = None

    try:
        await ensure_consumer_group(paper_redis)

        if settings.ENABLE_ORDERBOOK_SLIPPAGE:
            ob_exec_tasks = [
                asyncio.create_task(run_ob_exec_subscriber(ob_cache, connect_mds_redis, exchange))
                for exchange in sorted(supported_exchanges)
            ]
            ob_sync_task = asyncio.create_task(
                run_orderbook_sync_loop(db, mds_redis, settings.CONSUMER_NAME,
                                        supported_exchanges, settings.ORDERBOOK_SYNC_INTERVAL)
            )
        await snapshot_publisher.publish_all()
        snapshot_task = asyncio.create_task(snapshot_publisher.run())
        if ownership_monitor is not None:
            ownership_task = asyncio.create_task(ownership_monitor.run())

        if settings.ENABLE_WORKER_TPSL_AUTO_CLOSE:
            ticker_task = asyncio.create_task(
                run_ticker_subscriber(cache, connect_mds_redis, supported_exchanges)
            )
            price_check_task = asyncio.create_task(
                run_price_check_loop(db, executor, cache, ob_cache, fill_service)
            )
        else:
            logger.info("Worker auto TP/SL disabled (ENABLE_WORKER_TPSL_AUTO_CLOSE=False); alphas manage SL/TP via MDS price_alert")
        health_task = asyncio.create_task(run_health_loop(ownership_monitor))

        logger.info("Consumer started: stream=%s group=%s", settings.REDIS_STREAM, settings.CONSUMER_GROUP)

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
                    async def pre_open(signal):
                        exchange = str(signal.exchange).lower()
                        if not settings.OPEN_BOOK_PRE_SUBSCRIBE_ENABLED or exchange not in supported_exchanges:
                            return "unsupported_exchange"
                        try:
                            await publish_subscribe(mds_redis, exchange, settings.CONSUMER_NAME, signal.symbol)
                        except Exception:
                            logger.exception("[OPEN-PRE-SUBSCRIBE] publish failed symbol=%s", signal.symbol)
                            return "pre_subscribe_publish_failed"
                        return await ob_cache.wait_ready(
                            exchange, signal.symbol, settings.OPEN_BOOK_READY_TIMEOUT_MS / 1000.0
                        )

                    result = await process_signal_message(
                        data, db, executor, fill_service=fill_service,
                        snapshot_publisher=snapshot_publisher, pre_open=pre_open,
                    )
                    if result is not None:
                        logger.info("Processed %s signal: %s", data.get("type"), result)
                    exchange = str(data.get("exchange", "binance")).lower()
                    if (
                        result is not None
                        and data.get("type") == "OPEN"
                        and settings.ENABLE_ORDERBOOK_SLIPPAGE
                        and exchange in supported_exchanges
                    ):
                        try:
                            # Bounded so a stalled publish can't freeze the consumer loop.
                            await asyncio.wait_for(
                                publish_subscribe(mds_redis, exchange,
                                                  settings.CONSUMER_NAME, data.get("symbol", "")),
                                timeout=2.0,
                            )
                        except Exception as exc:
                            logger.warning("orderbook subscribe publish failed: %s", exc)
                    await paper_redis.xack(
                        settings.REDIS_STREAM,
                        settings.CONSUMER_GROUP,
                        msg_id,
                    )

    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        tasks = [task for task in (ticker_task, price_check_task, health_task,
                                   ob_sync_task, snapshot_task, ownership_task) if task is not None]
        tasks.extend(ob_exec_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if mds_redis is not None:
            try:
                await publish_empty_syncs(mds_redis, settings.CONSUMER_NAME, supported_exchanges)
            except Exception as exc:
                logger.warning("orderbook empty sync failed during shutdown: %s", exc)
            await mds_redis.aclose()
        await db.close()
        await paper_redis.aclose()


if __name__ == "__main__":
    install_uvloop_if_available()
    asyncio.run(run_consumer())
