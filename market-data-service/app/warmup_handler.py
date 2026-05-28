from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import redis as redis_lib

from app.aggregator import TF_MINUTES

if TYPE_CHECKING:
    from binance.async_client import AsyncClient
    from app.kline_feed import _RateLimiter

logger = logging.getLogger(__name__)


def _kline_weight(limit: int) -> int:
    if limit <= 100:
        return 1
    if limit <= 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


class WarmupHandler:
    def __init__(
        self,
        redis_client: redis_lib.Redis,
        client: "AsyncClient",
        rate_limiter: "_RateLimiter",
    ) -> None:
        self.redis = redis_client
        self._client = client
        self._rate_limiter = rate_limiter
        self._shutdown = asyncio.Event()
        self._group_name = "mds_warmup"
        self._consumer_name = "mds-warmup-1"

    async def _fetch_tf_candles(self, symbol: str, tf: str, bars: int) -> list[dict]:
        """Fetch TF candles directly from Binance REST + gap sync."""
        fetch_limit = bars + 2
        weight = _kline_weight(fetch_limit)

        await self._rate_limiter.acquire(weight=weight)
        klines = await self._client.futures_klines(symbol=symbol, interval=tf, limit=fetch_limit)
        now_ms = int(time.time() * 1000)
        closed = [k for k in klines if int(k[6]) < now_ms]
        if not closed:
            return []

        result = list(closed[-bars:])

        # Gap sync: if batch processing was slow, newer confirmed bars may have appeared
        tf_ms = TF_MINUTES.get(tf, 1) * 60 * 1000
        last_close = int(result[-1][6])
        if now_ms - last_close >= tf_ms:
            await self._rate_limiter.acquire(weight=1)
            gap = await self._client.futures_klines(
                symbol=symbol,
                interval=tf,
                startTime=last_close + 1,
                endTime=now_ms,
            )
            gap_closed = [k for k in gap if int(k[6]) < now_ms]
            if gap_closed:
                result = result + gap_closed
                logger.debug("[WARMUP] Gap sync %s %s: +%d bars", symbol, tf, len(gap_closed))

        return [
            {
                "symbol": symbol,
                "tf": tf,
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "open_time": int(r[0]),
                "close_time": int(r[6]),
                "confirmed": True,
            }
            for r in result
        ]

    async def _process_request(self, request: dict) -> None:
        alpha_id = request.get("alpha_id", "")
        tf = request.get("tf", "")
        bars = int(request.get("bars", "0"))
        symbols_str = request.get("symbols", "")
        if not alpha_id or not tf or not symbols_str:
            return

        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
        response_stream = f"warmup:response:{alpha_id}"
        semaphore = asyncio.Semaphore(20)

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                try:
                    candles = await self._fetch_tf_candles(symbol, tf, bars)
                except Exception as exc:
                    logger.warning("[WARMUP] Failed %s %s: %s", symbol, tf, exc)
                    candles = []
                self.redis.xadd(
                    response_stream,
                    {"symbol": symbol, "tf": tf, "candles": json.dumps(candles)},
                )

        await asyncio.gather(*(_fetch_one(s) for s in symbols))
        logger.info(
            "[WARMUP] Responded to %s: %d symbols at %s, %d bars",
            alpha_id, len(symbols), tf, bars,
        )

    async def run(self) -> None:
        stream = "warmup:request"
        try:
            self.redis.xgroup_create(stream, self._group_name, id="0", mkstream=True)
        except redis_lib.ResponseError:
            pass

        logger.info("[WARMUP] Handler ready, listening on %s", stream)

        while not self._shutdown.is_set():
            try:
                messages = await asyncio.to_thread(
                    self.redis.xreadgroup,
                    self._group_name,
                    self._consumer_name,
                    {stream: ">"},
                    count=1,
                    block=5000,
                )
                if not messages:
                    continue
                for _stream, entries in messages:
                    for msg_id, fields in entries:
                        await self._process_request(fields)
                        self.redis.xack(stream, self._group_name, msg_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[WARMUP] Error: %s", exc)
                await asyncio.sleep(1)

    def shutdown(self) -> None:
        self._shutdown.set()
