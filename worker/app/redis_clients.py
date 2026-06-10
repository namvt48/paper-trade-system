from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

import redis.asyncio as redis_lib

from app.config import settings

logger = logging.getLogger(__name__)


def _safe_host(url: str) -> str:
    return urlsplit(url).hostname or "unknown"


def make_mds_redis_client() -> redis_lib.Redis:
    """Create a lazy MDS Redis client without blocking paper-signal startup."""
    return redis_lib.from_url(
        settings.MDS_REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=settings.MDS_REDIS_CONNECT_TIMEOUT,
    )


async def _connect(url: str, dependency: str,
                   socket_connect_timeout: float | None = None) -> redis_lib.Redis:
    attempt = 0
    while True:
        attempt += 1
        client = redis_lib.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=socket_connect_timeout,
        )
        try:
            await client.ping()
            logger.info("%s Redis connected: %s", dependency, _safe_host(url))
            return client
        except redis_lib.RedisError as exc:
            await client.aclose()
            wait = min(attempt, 10)
            logger.warning(
                "%s Redis unavailable (%s): %s. Retry in %ss",
                dependency,
                _safe_host(url),
                exc,
                wait,
            )
            await asyncio.sleep(wait)


async def connect_paper_redis() -> redis_lib.Redis:
    return await _connect(settings.REDIS_URL, "paper")


async def connect_mds_redis() -> redis_lib.Redis:
    return await _connect(
        settings.MDS_REDIS_URL,
        "MDS",
        socket_connect_timeout=settings.MDS_REDIS_CONNECT_TIMEOUT,
    )
