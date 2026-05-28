from __future__ import annotations

import asyncio
import logging
import random
import time

try:
    from binance import BinanceSocketManager
    from binance.async_client import AsyncClient
    from binance.enums import FuturesType
except ImportError:  # pragma: no cover - dependency is installed in the service image
    BinanceSocketManager = None
    AsyncClient = object
    FuturesType = None

from app.aggregator import Aggregator
from app.models import KlineCandle

logger = logging.getLogger(__name__)


class _RateLimiter:
    """Token-bucket rate limiter for Binance REST weight limits.

    Supports feedback from X-MBX-USED-WEIGHT-1M response header so actual
    server-side consumption overrides the local estimate.
    """

    def __init__(self, max_weight_per_minute: int = 2400) -> None:
        self._lock = asyncio.Lock()
        self._rate_per_sec = max_weight_per_minute / 60.0
        self._tokens = 0.0  # start empty to prevent initial burst
        self._max = float(max_weight_per_minute)
        self._last = time.monotonic()

    async def acquire(self, weight: int = 10) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._max, self._tokens + (now - self._last) * self._rate_per_sec)
            self._last = now
            if self._tokens < weight:
                wait = (weight - self._tokens) / self._rate_per_sec
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= weight

    def sync_from_header(self, used_weight: int) -> None:
        """Adjust token bucket based on X-MBX-USED-WEIGHT-1M header value."""
        remaining = max(0.0, self._max - used_weight)
        if remaining < self._tokens:
            self._tokens = remaining
            if used_weight > self._max * 0.9:
                logger.warning("[RATE] Used weight at %d/%d (%.0f%%) — throttling", used_weight, int(self._max), used_weight / self._max * 100)


class KlineFeed:
    def __init__(self, aggregator: Aggregator, ws_batch_size: int = 150) -> None:
        self.aggregator = aggregator
        self.ws_batch_size = ws_batch_size
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=1000)
        self._shutdown = asyncio.Event()
        self._rate_limiter = _RateLimiter(max_weight_per_minute=2000)  # 83% of 2400 limit, headroom for reconciler

    def build_stream_names(self, symbols: list[str], tf: str = "1m") -> list[str]:
        return [f"{symbol.lower()}@kline_{tf}" for symbol in symbols]

    def batch_symbols(self, symbols: list[str]) -> list[list[str]]:
        return [symbols[i : i + self.ws_batch_size] for i in range(0, len(symbols), self.ws_batch_size)]

    async def process_message(self, msg_data: dict) -> list[KlineCandle] | None:
        candle = KlineCandle.from_ws_1m(msg_data)
        if candle is None:
            return None
        return self.aggregator.on_1m_close(candle)

    async def load_initial_data(
        self,
        client: AsyncClient,
        symbols: list[str],
        store_size: int = 500,
        semaphore_limit: int = 25,
    ) -> None:
        semaphore = asyncio.Semaphore(semaphore_limit)

        async def _load_one(symbol: str) -> None:
            async with semaphore:
                try:
                    klines = await self._fetch_initial_1m_klines(client, symbol, store_size)
                    if not klines:
                        return
                    for row in klines:
                        self.aggregator.on_1m_close(
                            KlineCandle(
                                symbol=symbol,
                                tf="1m",
                                open=float(row[1]),
                                high=float(row[2]),
                                low=float(row[3]),
                                close=float(row[4]),
                                volume=float(row[5]),
                                open_time=int(row[0]),
                                close_time=int(row[6]),
                                confirmed=True,
                            )
                        )
                except Exception as exc:
                    logger.warning("Failed to load initial data for %s: %s", symbol, exc)

        await asyncio.gather(*(_load_one(symbol) for symbol in symbols))
        logger.info("[KLINE] Initial 1m data loaded for %d symbols", len(symbols))

    async def _fetch_initial_1m_klines(self, client: AsyncClient, symbol: str, store_size: int) -> list:
        max_limit = 1500
        remaining = store_size
        end_time = None
        rows = []

        while remaining > 0:
            limit = min(remaining, max_limit)
            kwargs = {"symbol": symbol, "interval": "1m", "limit": limit}
            if end_time is not None:
                kwargs["endTime"] = end_time

            await self._rate_limiter.acquire(weight=10)
            chunk = await client.futures_klines(**kwargs)
            try:
                resp = getattr(client, "response", None)
                if resp is not None and not asyncio.iscoroutine(resp):
                    raw = resp.headers.get("X-MBX-USED-WEIGHT-1M")
                    if raw and isinstance(raw, str):
                        self._rate_limiter.sync_from_header(int(raw))
            except Exception:
                pass
            if not chunk:
                break

            rows = list(chunk) + rows
            remaining -= len(chunk)
            first_open_time = int(chunk[0][0])
            next_end_time = first_open_time - 1
            if end_time == next_end_time or len(chunk) < limit:
                break
            end_time = next_end_time

        now_ms = int(time.time() * 1000)
        closed_rows = [row for row in rows if int(row[6]) < now_ms]
        return closed_rows[-store_size:]

    async def run_ws_batch(self, client: AsyncClient, symbols: list[str], batch_id: int = 0) -> None:
        if BinanceSocketManager is None or FuturesType is None:
            raise RuntimeError("python-binance is required to run KlineFeed")

        stream_names = self.build_stream_names(symbols, "1m")
        consecutive_failures = 0

        while not self._shutdown.is_set():
            try:
                stagger = batch_id * 0.5 if consecutive_failures == 0 else random.uniform(2, 10)
                if stagger > 0:
                    await asyncio.sleep(stagger)
                if self._shutdown.is_set():
                    break

                socket_manager = BinanceSocketManager(client, max_queue_size=2000)
                logger.info("[KLINE] Batch %s connecting with %d streams", batch_id, len(stream_names))

                async with socket_manager.futures_multiplex_socket(
                    stream_names, futures_type=FuturesType.USD_M
                ) as ws:
                    logger.info("[KLINE] Batch %s connected", batch_id)
                    consecutive_failures = 0
                    last_msg_time = time.monotonic()

                    while not self._shutdown.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            if msg is None or msg.get("e") == "error":
                                continue
                            last_msg_time = time.monotonic()
                            self._queue_message(msg)
                        except asyncio.TimeoutError:
                            if time.monotonic() - last_msg_time > 30:
                                logger.warning("[KLINE] Batch %s silent for 30s, reconnecting", batch_id)
                                break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                if self._shutdown.is_set():
                    break
                backoff = min(5 * (2 ** (consecutive_failures - 1)), 60)
                logger.error("[KLINE] Batch %s error: %s. Reconnect in %ss", batch_id, exc, backoff)
                await asyncio.sleep(backoff)

    async def consume_queue(self, publisher) -> None:
        while not self._shutdown.is_set():
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                if msg is None:
                    continue
                results = await self.process_message(msg)
                if results:
                    for candle in results:
                        publisher.publish_kline(candle)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[KLINE] Queue consumer error: %s", exc, exc_info=True)

    def shutdown(self) -> None:
        self._shutdown.set()

    def _queue_message(self, msg: dict) -> None:
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(msg)
