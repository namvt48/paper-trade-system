from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import websockets

from app.models import TickerUpdate

logger = logging.getLogger(__name__)

BINANCE_STREAM_URL = "wss://fstream.binance.com/stream?streams="


class TickerFeed:
    def __init__(self, batch_size: int = 150):
        self.batch_size = batch_size
        self._shutdown = asyncio.Event()

    def build_ticker_streams(self, symbols: list[str]) -> list[str]:
        return [f"{symbol.lower()}@ticker" for symbol in symbols]

    def batch_symbols(self, symbols: list[str]) -> list[list[str]]:
        return [symbols[i : i + self.batch_size] for i in range(0, len(symbols), self.batch_size)]

    def parse_binance_ticker(self, msg: dict) -> TickerUpdate | None:
        payload = msg.get("data", msg)
        if payload.get("e") != "24hrTicker":
            return None
        if "s" not in payload or "c" not in payload:
            return None
        return TickerUpdate.from_binance_ws(payload)

    async def run_binance_batch(self, symbols: list[str], publisher) -> None:
        streams = self.build_ticker_streams(symbols)
        if not streams:
            return

        url = f"{BINANCE_STREAM_URL}{'/'.join(streams)}"
        consecutive_failures = 0

        while not self._shutdown.is_set():
            try:
                async with websockets.connect(url) as ws:
                    logger.info("[TICKER] Connected for %d symbols", len(symbols))
                    consecutive_failures = 0
                    last_msg_time = time.monotonic()

                    while not self._shutdown.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            last_msg_time = time.monotonic()
                            ticker = self.parse_binance_ticker(json.loads(raw))
                            if ticker:
                                publisher.publish_ticker(ticker)
                        except asyncio.TimeoutError:
                            if time.monotonic() - last_msg_time > 60:
                                logger.warning("[TICKER] Silent for 60s, reconnecting")
                                break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                if self._shutdown.is_set():
                    break
                backoff = min(5 * (2 ** (consecutive_failures - 1)), 60)
                logger.error("[TICKER] Error: %s. Reconnect in %ss", exc, backoff)
                await asyncio.sleep(backoff + random.random())

    def shutdown(self) -> None:
        self._shutdown.set()
