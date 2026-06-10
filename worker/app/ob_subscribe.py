from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


def _channel(exchange: str) -> str:
    return f"orderbook:subscribe:{exchange}"


async def publish_sync(redis_client, exchange: str, consumer_id: str, symbols: list[str]) -> None:
    await redis_client.publish(
        _channel(exchange),
        json.dumps({"consumer_id": consumer_id, "action": "sync", "symbols": list(symbols)}),
    )


async def publish_subscribe(redis_client, exchange: str, consumer_id: str, symbol: str) -> None:
    await redis_client.publish(
        _channel(exchange),
        json.dumps({"consumer_id": consumer_id, "action": "subscribe", "symbols": [symbol]}),
    )


async def publish_empty_syncs(redis_client, consumer_id: str, exchanges: set[str]) -> None:
    for exchange in sorted(exchanges):
        await publish_sync(redis_client, exchange, consumer_id, [])


async def run_orderbook_sync_loop(db, redis_client, consumer_id: str,
                                  supported_exchanges: set[str],
                                  interval: float = 5.0) -> None:
    """Periodically tell MDS which open-position symbols need a depth book."""
    while True:
        try:
            await asyncio.sleep(interval)
            by_exchange = await db.get_open_symbols_by_exchange()
            for exchange in sorted(supported_exchanges):
                await publish_sync(
                    redis_client, exchange, consumer_id,
                    sorted(by_exchange.get(exchange, set())),
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("[OB-SUB] sync loop error: %s", exc)
            await asyncio.sleep(5)
