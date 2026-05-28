from __future__ import annotations

import asyncio
import logging
import os
import signal as sig

import redis as redis_lib
import requests
from binance.async_client import AsyncClient

from app.aggregator import Aggregator
from app.config import settings
from app.kline_feed import KlineFeed
from app.publisher import Publisher
from app.reconciler import Reconciler
from app.ticker_feed import TickerFeed
from app.warmup_handler import WarmupHandler

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    app_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    handlers = [
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(settings.LOG_DIR, "market-data.log")),
    ]
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.handlers = handlers
    for name in ("app", "__main__"):
        logging.getLogger(name).setLevel(app_level)


def get_symbol_universe() -> list[str]:
    if settings.SYMBOL_MODE == "manual":
        return settings.get_symbols_list()

    try:
        response = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15)
        response.raise_for_status()
        data = response.json()
        symbols = [
            item["symbol"]
            for item in data.get("symbols", [])
            if item.get("quoteAsset") == "USDT"
            and item.get("contractType") == "PERPETUAL"
            and item.get("status") == "TRADING"
        ]
        return sorted(symbols)
    except Exception as exc:
        logger.warning("Failed to fetch symbol universe: %s", exc)
        return ["BTCUSDT", "ETHUSDT"]


async def run_service() -> None:
    configure_logging()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    for signal_name in (sig.SIGTERM, sig.SIGINT):
        loop.add_signal_handler(signal_name, shutdown_event.set)

    symbols = get_symbol_universe()
    logger.info("Symbol universe: %d symbols", len(symbols))

    aggregator = Aggregator(timeframes=settings.get_timeframes(), max_1m_per_symbol=settings.MAX_1M_BUFFER)
    redis_client = await connect_redis()
    publisher = Publisher(redis_client, snapshot_max_candles=settings.SNAPSHOT_MAX_CANDLES)
    publisher.publish_symbols(symbols)

    client = await AsyncClient.create()
    kline_feed = KlineFeed(aggregator=aggregator, ws_batch_size=settings.WS_BATCH_SIZE)
    ticker_feed = TickerFeed(batch_size=settings.TICKER_BATCH_SIZE)
    reconciler = Reconciler(
        aggregator=aggregator,
        reconcile_tfs=settings.get_reconcile_tfs(),
        reconcile_delay=settings.RECONCILE_DELAY,
        semaphore_limit=settings.REST_SEMAPHORE,
        rate_limiter=kline_feed._rate_limiter,
    )
    warmup_handler = WarmupHandler(redis_client, client, kline_feed._rate_limiter)

    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(_health_loop(shutdown_event)))
    tasks.append(asyncio.create_task(warmup_handler.run()))

    for batch_id, batch in enumerate(kline_feed.batch_symbols(symbols)):
        tasks.append(asyncio.create_task(kline_feed.run_ws_batch(client, batch, batch_id=batch_id)))
    tasks.append(asyncio.create_task(kline_feed.consume_queue(publisher)))

    for batch in ticker_feed.batch_symbols(symbols):
        tasks.append(asyncio.create_task(ticker_feed.run_binance_batch(batch, publisher)))

    tasks.append(asyncio.create_task(reconciler.run(client, symbols, publisher)))

    logger.info("Market data service running with %d tasks", len(tasks))
    waiter = asyncio.create_task(shutdown_event.wait())
    try:
        await waiter
    finally:
        logger.info("Shutting down market data service")
        kline_feed.shutdown()
        ticker_feed.shutdown()
        reconciler.shutdown()
        warmup_handler.shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close_connection()
        redis_client.close()


async def _health_loop(shutdown_event: asyncio.Event) -> None:
    while not shutdown_event.is_set():
        try:
            with open("/tmp/bot_health", "w") as health_file:
                health_file.write("ok")
        except Exception:
            pass
        await asyncio.sleep(10)


async def connect_redis() -> redis_lib.Redis:
    attempt = 0
    while True:
        attempt += 1
        client = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            client.ping()
            return client
        except redis_lib.RedisError as exc:
            client.close()
            wait = min(attempt, 10)
            logger.warning("Redis unavailable: %s. Retry in %ss", exc, wait)
            await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(run_service())
