import asyncio
import json
import logging
import os
import signal as sig
from abc import ABC, abstractmethod

import redis as redis_lib

from base import signal_push
from base.config import BaseConfig
from base.models import SymbolData


class BaseEngine(ABC):
    def __init__(self, config: BaseConfig):
        self.config = config
        self.symbol_data: dict[str, dict[str, SymbolData]] = {}
        self.data_lock = asyncio.Lock()
        self.shutdown_event = asyncio.Event()
        self._logger = logging.getLogger(config.ALPHA_ID)
        self._blacklist: set[str] = {
            s.strip().upper() for s in config.SYMBOL_BLACKLIST.split(",") if s.strip()
        }

    @abstractmethod
    def get_required_channels(self) -> list[str]:
        """Return Redis Pub/Sub channels needed by this alpha."""

    @abstractmethod
    async def scan_loop(self) -> None:
        """Main signal scanning loop; call push_signal() when signals are found."""

    @abstractmethod
    def _get_warmup_symbols(self) -> list[str]:
        """Return symbols to request warmup data for (already filtered for blacklist)."""

    async def on_warmup_complete(self) -> None:
        """Called after warmup data is loaded. Override to reconstruct in-memory state."""

    def _is_blacklisted(self, symbol: str) -> bool:
        return symbol in self._blacklist

    def on_kline_message(self, msg: dict) -> None:
        symbol = msg.get("symbol", "")
        tf = msg.get("tf", "")
        if not symbol or not tf:
            return

        if self._is_blacklisted(symbol):
            return

        if symbol not in self.symbol_data:
            self.symbol_data[symbol] = {}
        if tf not in self.symbol_data[symbol]:
            self.symbol_data[symbol][tf] = SymbolData()

        sd = self.symbol_data[symbol][tf]
        open_time = int(msg.get("open_time", 0))
        is_correction = bool(msg.get("correction", False))

        if is_correction:
            for index in range(len(sd.time_list) - 1, -1, -1):
                if sd.time_list[index] == open_time:
                    self._replace_candle(sd, index, msg)
                    return

        if sd.time_list and open_time <= sd.time_list[-1]:
            return

        sd.time_list.append(open_time)
        sd.open_list.append(float(msg.get("open", 0.0)))
        sd.high_list.append(float(msg.get("high", 0.0)))
        sd.low_list.append(float(msg.get("low", 0.0)))
        sd.price_list.append(float(msg.get("close", 0.0)))
        sd.volume_list.append(float(msg.get("volume", 0.0)))
        self._trim_symbol_data(sd)

    def _load_warmup_candles(self, data: dict) -> None:
        symbol = data.get("symbol", "")
        tf = data.get("tf", "")
        if not symbol or not tf:
            return
        if self._is_blacklisted(symbol):
            return

        candles_raw = data.get("candles", "[]")
        candles = json.loads(candles_raw) if isinstance(candles_raw, str) else candles_raw
        if not candles:
            return

        if symbol not in self.symbol_data:
            self.symbol_data[symbol] = {}
        if tf not in self.symbol_data[symbol]:
            self.symbol_data[symbol][tf] = SymbolData()

        sd = self.symbol_data[symbol][tf]
        for candle in candles:
            open_time = int(candle.get("open_time", 0))
            if sd.time_list and open_time <= sd.time_list[-1]:
                continue
            sd.time_list.append(open_time)
            sd.open_list.append(float(candle.get("open", 0.0)))
            sd.high_list.append(float(candle.get("high", 0.0)))
            sd.low_list.append(float(candle.get("low", 0.0)))
            sd.price_list.append(float(candle.get("close", 0.0)))
            sd.volume_list.append(float(candle.get("volume", 0.0)))

        self._trim_symbol_data(sd)

    async def _request_warmup(self) -> None:
        symbols = self._get_warmup_symbols()
        if not symbols:
            self._logger.warning("[%s] No warmup symbols, skipping warmup", self.config.ALPHA_ID)
            return

        tf = getattr(self.config, "TF", "")
        bars = self.config.WARMUP_BARS

        redis_client = await self._connect_redis()
        try:
            response_stream = f"warmup:response:{self.config.ALPHA_ID}"
            # Create group BEFORE sending the request to avoid the race condition where
            # MDS responds before the group exists. id="$" ensures we only receive
            # responses to THIS request, not stale messages from previous runs.
            try:
                redis_client.xgroup_create(response_stream, "alpha_consumer", id="$", mkstream=True)
            except redis_lib.ResponseError:
                redis_client.xtrim(response_stream, maxlen=0)

            redis_client.xadd(
                "warmup:request",
                {
                    "alpha_id": self.config.ALPHA_ID,
                    "tf": tf,
                    "bars": str(bars),
                    "symbols": ",".join(symbols),
                },
            )

            timeout_sec = float(getattr(self.config, "INITIAL_DATA_TIMEOUT_SEC", 30.0))
            deadline = asyncio.get_running_loop().time() + timeout_sec
            received_symbols: set[str] = set()
            expected = set(symbols)

            while received_symbols != expected:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._logger.warning(
                        "[%s] Warmup timeout: %d/%d symbols received",
                        self.config.ALPHA_ID,
                        len(received_symbols),
                        len(expected),
                    )
                    break

                messages = await asyncio.to_thread(
                    redis_client.xreadgroup,
                    "alpha_consumer",
                    self.config.ALPHA_ID,
                    {response_stream: ">"},
                    count=len(expected),
                    block=int(min(remaining, 5) * 1000),
                )

                if not messages:
                    continue

                for _stream, entries in messages:
                    for _msg_id, fields in entries:
                        self._load_warmup_candles(fields)
                        sym = fields.get("symbol", "")
                        if sym:
                            received_symbols.add(sym)

            self._logger.info(
                "[%s] Warmup complete: %d/%d symbols loaded",
                self.config.ALPHA_ID,
                len(received_symbols),
                len(expected),
            )
            try:
                redis_client.delete(response_stream)
            except Exception:
                pass
        finally:
            redis_client.close()

    async def subscribe_data_feeds(self) -> asyncio.Task:
        redis_client = await self._connect_redis()
        channels = self.get_required_channels()

        pubsub = redis_client.pubsub()
        pubsub.subscribe(*channels)
        self._logger.info("[%s] Subscribed to data channels: %s", self.config.ALPHA_ID, channels)

        async def _listen() -> None:
            try:
                while not self.shutdown_event.is_set():
                    try:
                        msg = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
                        if not msg or msg["type"] != "message":
                            continue
                        channel = msg["channel"]
                        if isinstance(channel, bytes):
                            channel = channel.decode()
                        if channel.startswith("kline:"):
                            self.on_kline_message(json.loads(msg["data"]))
                    except Exception as exc:
                        self._logger.debug("Redis subscriber error: %s", exc)
                        await asyncio.sleep(1)
            finally:
                pubsub.unsubscribe()
                pubsub.close()
                redis_client.close()

        return asyncio.create_task(_listen())

    async def _connect_redis(self) -> redis_lib.Redis:
        attempt = 0
        while not self.shutdown_event.is_set():
            attempt += 1
            redis_client = redis_lib.from_url(self.config.REDIS_URL, decode_responses=True)
            try:
                redis_client.ping()
                return redis_client
            except redis_lib.RedisError as exc:
                redis_client.close()
                wait = min(attempt, 10)
                self._logger.warning("Redis unavailable: %s. Retry in %ss", exc, wait)
                await asyncio.sleep(wait)
        raise asyncio.CancelledError

    async def run(self) -> None:
        os.makedirs(self.config.LOG_DIR, exist_ok=True)
        app_level = getattr(logging, self.config.LOG_LEVEL.upper(), logging.INFO)
        handlers = [
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(self.config.LOG_DIR, "bot.log")),
        ]
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        for h in handlers:
            h.setFormatter(fmt)
        root = logging.getLogger()
        root.setLevel(logging.WARNING)
        root.handlers = handlers
        for name in (self.config.ALPHA_ID, "base", "app", "__main__"):
            logging.getLogger(name).setLevel(app_level)

        signal_push.init(self.config.REDIS_URL, self.config.REDIS_STREAM)

        loop = asyncio.get_running_loop()
        for signal_name in (sig.SIGTERM, sig.SIGINT):
            loop.add_signal_handler(signal_name, self.shutdown_event.set)

        self._logger.info("[%s] Starting alpha engine", self.config.ALPHA_ID)

        try:
            await self._request_warmup()
        except Exception as exc:
            self._logger.warning("[%s] Warmup failed: %s", self.config.ALPHA_ID, exc)

        try:
            await self.on_warmup_complete()
        except Exception as exc:
            self._logger.warning("[%s] on_warmup_complete error: %s", self.config.ALPHA_ID, exc)

        sub_task = await self.subscribe_data_feeds()

        timeout_sec = float(getattr(self.config, "INITIAL_DATA_TIMEOUT_SEC", 30.0))
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while len(self.symbol_data) == 0 and not self.shutdown_event.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(1)

        tf = getattr(self.config, "TF", "")
        if len(self.symbol_data) == 0:
            self._logger.warning(
                "[%s] No market data after %.1fs; starting scan loop and waiting for live data.",
                self.config.ALPHA_ID,
                timeout_sec,
            )
        else:
            total_candles = sum(
                len(tf_map.get(tf, SymbolData()).price_list)
                for tf_map in self.symbol_data.values()
            )
            self._logger.info(
                "[%s] Ready: %d symbols, %d candles at %s",
                self.config.ALPHA_ID,
                len(self.symbol_data),
                total_candles,
                tf,
            )

        scan_task = asyncio.create_task(self.scan_loop())
        health_task = asyncio.create_task(self._health_loop())

        try:
            await asyncio.gather(scan_task, health_task, sub_task)
        except asyncio.CancelledError:
            pass
        finally:
            self.shutdown_event.set()
            for task in (scan_task, health_task, sub_task):
                task.cancel()
            await asyncio.gather(scan_task, health_task, sub_task, return_exceptions=True)
            self._logger.info("[%s] Shutting down", self.config.ALPHA_ID)

    async def _health_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                with open("/tmp/bot_health", "w") as health_file:
                    health_file.write("ok")
            except Exception:
                pass
            await asyncio.sleep(10)

    def push_signal(self, signal_type: str, **kwargs) -> None:
        signal_push.push_signal(signal_type, self.config.ALPHA_ID, **kwargs)

    def _replace_candle(self, sd: SymbolData, index: int, msg: dict) -> None:
        sd.open_list[index] = float(msg.get("open", 0.0))
        sd.high_list[index] = float(msg.get("high", 0.0))
        sd.low_list[index] = float(msg.get("low", 0.0))
        sd.price_list[index] = float(msg.get("close", 0.0))
        sd.volume_list[index] = float(msg.get("volume", 0.0))

    def _trim_symbol_data(self, sd: SymbolData) -> None:
        max_candles = getattr(self.config, "DATA_MAX_CANDLES", 1000)
        overflow = len(sd.time_list) - max_candles
        if overflow <= 0:
            return
        del sd.time_list[:overflow]
        del sd.open_list[:overflow]
        del sd.high_list[:overflow]
        del sd.low_list[:overflow]
        del sd.price_list[:overflow]
        del sd.volume_list[:overflow]
