import asyncio
import json
import logging
import os

import redis as redis_lib

from app.config import settings
from app.db import Database
from app.executor import Executor
from app.models import SignalType, parse_signal

logger = logging.getLogger(__name__)


class TickerPriceCache:
    def __init__(self):
        self._prices: dict[str, float] = {}

    def update_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def get_prices(self, symbols: list[str] | None = None) -> dict[str, float]:
        if symbols is None:
            return dict(self._prices)
        return {symbol: self._prices[symbol] for symbol in symbols if symbol in self._prices}


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


async def process_signal_message(data: dict, db: Database, executor: Executor) -> dict | None:
    signal_id = data.get("signal_id", "unknown")
    alpha_id = data.get("alpha_id", "unknown")
    signal_type = data.get("type", "unknown")

    await db.log_signal(
        signal_id=signal_id,
        alpha_id=alpha_id,
        signal_type=signal_type,
        payload=json.dumps(data),
    )

    try:
        signal = parse_signal(data)

        if signal.type == SignalType.OPEN:
            result = await executor.process_open(signal)
        elif signal.type == SignalType.MODIFY:
            result = await executor.process_modify(signal)
        elif signal.type == SignalType.CLOSE:
            result = await executor.process_close(signal)
        else:
            result = None

        await db.mark_signal_processed(signal_id)
        return result

    except Exception as exc:
        logger.error("Error processing signal %s: %s", signal_id, exc)
        await db.mark_signal_processed(signal_id, error=str(exc))
        return None


async def run_ticker_subscriber(cache: TickerPriceCache) -> None:
    redis_client = await connect_redis()
    pubsub = redis_client.pubsub()
    pubsub.subscribe("ticker")
    logger.info("[TICKER] Subscribed to Redis ticker channel")

    try:
        while True:
            try:
                msg = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
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
        pubsub.unsubscribe()
        pubsub.close()
        redis_client.close()


async def run_price_check_loop(db: Database, executor: Executor, cache: TickerPriceCache) -> None:
    while True:
        try:
            await asyncio.sleep(settings.PRICE_CHECK_INTERVAL)
            symbols = await db.get_symbols_with_open_positions()
            prices = cache.get_prices(symbols)
            if not prices:
                continue

            hits = await executor.check_tpsl_hits(prices)
            for hit in hits:
                logger.info("[TPSL] Auto-closed: %s", hit)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Price check error: %s", exc, exc_info=True)
            await asyncio.sleep(5)


async def run_health_loop() -> None:
    while True:
        try:
            with open("/tmp/bot_health", "w") as health_file:
                health_file.write("ok")
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


async def connect_redis() -> redis_lib.Redis:
    attempt = 0
    while True:
        attempt += 1
        redis_client = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            redis_client.ping()
            return redis_client
        except redis_lib.RedisError as exc:
            redis_client.close()
            wait = min(attempt, 10)
            logger.warning("Redis unavailable: %s. Retry in %ss", exc, wait)
            await asyncio.sleep(wait)


async def run_consumer():
    configure_logging()

    db = Database(settings.DB_PATH)
    await db.init()
    await register_configured_alphas(db)

    executor = Executor(
        db,
        slippage_pct=settings.SLIPPAGE_PCT,
        duplicate_policy=settings.DUPLICATE_POSITION_POLICY,
    )
    cache = TickerPriceCache()

    redis_client = await connect_redis()
    ticker_task = None
    price_check_task = None
    health_task = None

    try:
        try:
            redis_client.xgroup_create(settings.REDIS_STREAM, settings.CONSUMER_GROUP, id="0", mkstream=True)
        except redis_lib.ResponseError:
            pass

        ticker_task = asyncio.create_task(run_ticker_subscriber(cache))
        price_check_task = asyncio.create_task(run_price_check_loop(db, executor, cache))
        health_task = asyncio.create_task(run_health_loop())

        logger.info("Consumer started: stream=%s group=%s", settings.REDIS_STREAM, settings.CONSUMER_GROUP)

        while True:
            messages = await asyncio.to_thread(
                redis_client.xreadgroup,
                settings.CONSUMER_GROUP,
                settings.CONSUMER_NAME,
                {settings.REDIS_STREAM: ">"},
                count=10,
                block=1000,
            )

            if not messages:
                continue

            for _, msgs in messages:
                for msg_id, data in msgs:
                    result = await process_signal_message(data, db, executor)
                    if result is not None:
                        logger.info("Processed %s signal: %s", data.get("type"), result)
                    await asyncio.to_thread(
                        redis_client.xack,
                        settings.REDIS_STREAM,
                        settings.CONSUMER_GROUP,
                        msg_id,
                    )

    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        tasks = [task for task in (ticker_task, price_check_task, health_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()
        redis_client.close()


if __name__ == "__main__":
    asyncio.run(run_consumer())
