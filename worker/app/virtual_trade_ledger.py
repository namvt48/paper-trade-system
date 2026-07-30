from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as redis_lib

from app.db import Database

logger = logging.getLogger(__name__)


def _contains_rows(messages) -> bool:
    return any(bool(rows) for _, rows in messages)


async def process_virtual_trade_message(data: dict, db: Database) -> dict | None:
    raw = data.get("payload")
    event = json.loads(raw) if isinstance(raw, str) else data
    if not isinstance(event, dict):
        raise ValueError("virtual ledger payload must be an object")
    if event.get("ledger_mode") != "virtual":
        raise ValueError("virtual ledger message requires ledger_mode=virtual")
    required = {
        "type",
        "event_id",
        "position_id",
        "alpha_id",
        "symbol",
        "side",
        "price",
        "qty",
        "timestamp",
    }
    missing = sorted(required - set(event))
    if missing:
        raise ValueError(f"virtual ledger message missing fields: {missing}")
    if event["type"] not in {"VIRTUAL_OPEN", "VIRTUAL_CLOSE"}:
        raise ValueError(f"unsupported virtual ledger type: {event['type']}")
    if event["side"] not in {"LONG", "SHORT"}:
        raise ValueError(f"unsupported virtual ledger side: {event['side']}")
    if float(event["price"]) <= 0 or float(event["qty"]) <= 0:
        raise ValueError("virtual ledger price and qty must be positive")
    return await db.apply_virtual_trade_event(event)


async def ensure_virtual_consumer_group(
    redis_client, stream: str, group: str
) -> None:
    try:
        await redis_client.xgroup_create(stream, group, id="0", mkstream=True)
    except redis_lib.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def run_virtual_trade_consumer(
    *,
    connect_redis,
    db_path: str,
    stream: str,
    group: str,
    consumer: str,
    read_count: int,
    block_ms: int,
) -> None:
    db = Database(db_path)
    await db.init()
    try:
        while True:
            redis_client = None
            try:
                redis_client = await connect_redis()
                await ensure_virtual_consumer_group(redis_client, stream, group)
                logger.info(
                    "[VIRTUAL-LEDGER] consumer started stream=%s group=%s",
                    stream,
                    group,
                )
                recover_pending = True
                while True:
                    messages = await redis_client.xreadgroup(
                        group,
                        consumer,
                        {stream: "0" if recover_pending else ">"},
                        count=read_count,
                        block=block_ms,
                    )
                    if recover_pending and not _contains_rows(messages):
                        recover_pending = False
                        continue
                    for _, rows in messages:
                        for message_id, data in rows:
                            try:
                                result = await process_virtual_trade_message(data, db)
                                if result is not None:
                                    logger.info(
                                        "[VIRTUAL-LEDGER] committed %s", result
                                    )
                            except Exception:
                                logger.exception(
                                    "[VIRTUAL-LEDGER] failed message=%s",
                                    message_id,
                                )
                                continue
                            await redis_client.xack(stream, group, message_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[VIRTUAL-LEDGER] consumer connection failed")
                await asyncio.sleep(5)
            finally:
                if redis_client is not None:
                    await redis_client.aclose()
    finally:
        await db.close()
