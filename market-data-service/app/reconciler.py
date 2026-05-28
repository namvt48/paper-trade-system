from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

try:
    from binance.async_client import AsyncClient
except ImportError:  # pragma: no cover - dependency is installed in the service image
    AsyncClient = object

if TYPE_CHECKING:
    from app.kline_feed import _RateLimiter

from app.aggregator import Aggregator, TF_MINUTES
from app.models import KlineCandle

logger = logging.getLogger(__name__)

_KLINE_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


class Reconciler:
    def __init__(
        self,
        aggregator: Aggregator,
        reconcile_tfs: list[str] | None = None,
        reconcile_delay: int = 5,
        semaphore_limit: int = 25,
        rate_limiter: "_RateLimiter | None" = None,
    ):
        self.aggregator = aggregator
        self.reconcile_tfs = reconcile_tfs or ["15m", "1h"]
        self.reconcile_delay = reconcile_delay
        self.semaphore_limit = semaphore_limit
        self._rate_limiter = rate_limiter
        self._shutdown = asyncio.Event()

    def is_candle_boundary(self, open_time_ms: int, tf: str) -> bool:
        tf_minutes = TF_MINUTES.get(tf, 1)
        return open_time_ms % (tf_minutes * 60 * 1000) == 0

    def should_reconcile(self, open_time_ms: int) -> bool:
        return any(self.is_candle_boundary(open_time_ms, tf) for tf in self.reconcile_tfs)

    async def reconcile_symbol(self, client: AsyncClient, symbol: str, tf: str) -> list[KlineCandle]:
        corrections: list[KlineCandle] = []
        interval = _KLINE_INTERVAL.get(tf, tf)

        try:
            if self._rate_limiter:
                await self._rate_limiter.acquire(weight=1)
            klines = await client.futures_klines(symbol=symbol, interval=interval, limit=2)
            if not klines or len(klines) < 2:
                return corrections

            row = klines[-2]
            rest_candle = KlineCandle(
                symbol=symbol,
                tf=tf,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                open_time=int(row[0]),
                close_time=int(row[6]),
                confirmed=True,
                correction=True,
            )

            for stored in self.aggregator.get_candles(symbol, tf):
                if stored.open_time != rest_candle.open_time:
                    continue
                if self._differs(stored, rest_candle):
                    self.aggregator.apply_correction(rest_candle)
                    corrections.append(rest_candle)
                break
        except Exception as exc:
            logger.debug("[RECONCILE] Failed for %s %s: %s", symbol, tf, exc)

        return corrections

    async def reconcile_all(self, client: AsyncClient, symbols: list[str], tf: str, publisher=None) -> int:
        semaphore = asyncio.Semaphore(self.semaphore_limit)

        async def _reconcile_one(symbol: str) -> int:
            async with semaphore:
                corrections = await self.reconcile_symbol(client, symbol, tf)
                if corrections and publisher:
                    for correction in corrections:
                        publisher.publish_kline(correction)
                return len(corrections)

        results = await asyncio.gather(*(_reconcile_one(symbol) for symbol in symbols))
        total = sum(results)
        if total:
            logger.info("[RECONCILE] %s: %d corrections for %d symbols", tf, total, len(symbols))
        return total

    async def run(self, client: AsyncClient, symbols: list[str], publisher=None) -> None:
        tasks = [
            asyncio.create_task(self._run_for_tf(client, symbols, tf, publisher))
            for tf in self.reconcile_tfs
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_for_tf(self, client: AsyncClient, symbols: list[str], tf: str, publisher=None) -> None:
        tf_seconds = TF_MINUTES.get(tf, 60) * 60
        while not self._shutdown.is_set():
            try:
                now = time.time()
                next_boundary = (int(now // tf_seconds) + 1) * tf_seconds
                wait = next_boundary - now + self.reconcile_delay
                if wait > 0:
                    await asyncio.sleep(wait)
                if self._shutdown.is_set():
                    break
                await self.reconcile_all(client, symbols, tf, publisher)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[RECONCILE] %s error: %s", tf, exc)
                await asyncio.sleep(10)

    def shutdown(self) -> None:
        self._shutdown.set()

    @staticmethod
    def _differs(stored: KlineCandle, rest: KlineCandle) -> bool:
        return (
            stored.open != rest.open
            or stored.high != rest.high
            or stored.low != rest.low
            or stored.close != rest.close
            or stored.volume != rest.volume
        )
