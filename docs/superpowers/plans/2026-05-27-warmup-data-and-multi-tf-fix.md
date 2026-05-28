# Warmup Data Layer & Multi-TF Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a warmup data layer so alphas can request historical candles from MDS at startup, fix the multi-TF bug in BaseEngine by restructuring symbol_data to be keyed by symbol+TF, and add symbol blacklisting.

**Architecture:** Alpha sends warmup request via Redis Stream `warmup:request`, MDS responds via `warmup:response:{alpha_id}` with candle arrays from its aggregator. `symbol_data` changes from `dict[str, SymbolData]` to `dict[str, dict[str, SymbolData]]`. Blacklist is alpha-side only — MDS is unaware.

**Tech Stack:** Python 3.12, redis[hiredis]>=5.0, pydantic-settings>=2.0, pytest

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `alphas/base/symbol_utils.py` | Create | `get_binance_perp_symbols()`, `get_top_n_binance_perps()` — moved from deleted `market_data.py` |
| `alphas/base/config.py` | Modify | Add `WARMUP_BARS`, `SYMBOL_BLACKLIST` fields |
| `alphas/base/engine.py` | Modify | Multi-TF `symbol_data`, `on_kline_message` fix, `_request_warmup()`, `_load_warmup_candles()`, `_is_blacklisted()`, abstract `_get_warmup_symbols()`, remove `_load_snapshots()` |
| `alphas/base/tests/test_engine.py` | Modify | Update existing tests for multi-TF, add warmup/blacklist tests |
| `alphas/base/tests/test_symbol_utils.py` | Create | Tests for symbol utility functions |
| `alphas/adx-trend-follow/app/engine.py` | Modify | `_get_warmup_symbols()`, multi-TF `symbol_data` access pattern |
| `alphas/wilder/app/engine.py` | Modify | `_get_warmup_symbols()`, multi-TF `symbol_data` access pattern |
| `market-data-service/app/warmup_handler.py` | Create | MDS-side warmup request handler |
| `market-data-service/app/main.py` | Modify | Add warmup handler task |
| `market-data-service/tests/test_warmup_handler.py` | Create | Tests for warmup handler |

---

### Task 1: Create `alphas/base/symbol_utils.py`

**Files:**
- Create: `alphas/base/symbol_utils.py`
- Create: `alphas/base/tests/test_symbol_utils.py`

- [ ] **Step 1: Write the failing test**

```python
# alphas/base/tests/test_symbol_utils.py
from unittest.mock import patch

from base.symbol_utils import get_binance_perp_symbols, get_top_n_binance_perps


@patch("base.symbol_utils.requests.get")
def test_get_binance_perp_symbols(mock_get):
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "symbols": [
            {"symbol": "BTCUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING"},
            {"symbol": "ETHUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING"},
            {"symbol": "BTCBUSD", "quoteAsset": "BUSD", "contractType": "PERPETUAL", "status": "TRADING"},
            {"symbol": "EXPIRED", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "DELISTED"},
        ]
    }
    result = get_binance_perp_symbols()
    assert result == ["BTCUSDT", "ETHUSDT"]


@patch("base.symbol_utils.requests.get")
def test_get_binance_perp_symbols_fallback(mock_get):
    mock_get.side_effect = Exception("network error")
    result = get_binance_perp_symbols()
    assert result == ["BTCUSDT", "ETHUSDT"]


@patch("base.symbol_utils.get_binance_perp_symbols")
def test_get_top_n_binance_perps(mock_get_symbols):
    mock_get_symbols.return_value = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
    result = get_top_n_binance_perps(2)
    assert result == ["AAAUSDT", "BBBUSDT"]


@patch("base.symbol_utils.get_binance_perp_symbols")
def test_get_top_n_binance_perps_all(mock_get_symbols):
    mock_get_symbols.return_value = ["AAAUSDT", "BBBUSDT"]
    result = get_top_n_binance_perps(10)
    assert result == ["AAAUSDT", "BBBUSDT"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -m pytest alphas/base/tests/test_symbol_utils.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'base.symbol_utils'`

- [ ] **Step 3: Write implementation**

```python
# alphas/base/symbol_utils.py
from __future__ import annotations

import logging
import requests

logger = logging.getLogger(__name__)


def get_binance_perp_symbols() -> list[str]:
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
        logger.warning("Failed to fetch Binance perp symbols: %s", exc)
        return ["BTCUSDT", "ETHUSDT"]


def get_top_n_binance_perps(n: int) -> list[str]:
    symbols = get_binance_perp_symbols()
    return symbols[:n]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -m pytest alphas/base/tests/test_symbol_utils.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add alphas/base/symbol_utils.py alphas/base/tests/test_symbol_utils.py
git commit -m "feat: add symbol_utils with get_binance_perp_symbols and get_top_n_binance_perps"
```

---

### Task 2: Update BaseConfig with WARMUP_BARS and SYMBOL_BLACKLIST

**Files:**
- Modify: `alphas/base/config.py`

- [ ] **Step 1: Update BaseConfig**

Add two new fields to `BaseConfig` after `INITIAL_DATA_TIMEOUT_SEC`:

```python
# alphas/base/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseConfig(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379"
    REDIS_STREAM: str = "paper-signals"
    ALPHA_ID: str = ""
    INVEST_PER_TRADE: float = 100.0
    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"
    EXCHANGE: str = "binance"
    FEE_PCT: float = 0.0005
    DATA_CHANNELS: str = ""
    DATA_MAX_CANDLES: int = 1000
    INITIAL_DATA_TIMEOUT_SEC: float = 30.0
    WARMUP_BARS: int = 50
    SYMBOL_BLACKLIST: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )
```

- [ ] **Step 2: Verify**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -c "from alphas.base.config import BaseConfig; c = BaseConfig(); print(c.WARMUP_BARS, c.SYMBOL_BLACKLIST)"`
Expected: `50 ` (50 and empty string)

- [ ] **Step 3: Commit**

```bash
git add alphas/base/config.py
git commit -m "feat: add WARMUP_BARS and SYMBOL_BLACKLIST to BaseConfig"
```

---

### Task 3: Fix BaseEngine — multi-TF symbol_data, blacklist, warmup, remove _load_snapshots

**Files:**
- Modify: `alphas/base/engine.py`
- Modify: `alphas/base/tests/test_engine.py`

This is the largest task. It restructures `symbol_data`, fixes `on_kline_message`, adds `_is_blacklisted`, `_request_warmup`, `_load_warmup_candles`, abstract `_get_warmup_symbols`, and removes `_load_snapshots`.

- [ ] **Step 1: Write failing tests for multi-TF symbol_data and blacklist**

Replace the entire test file:

```python
# alphas/base/tests/test_engine.py
import json
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from base.config import BaseConfig
from base.engine import BaseEngine
from base.models import SymbolData


class MockEngine(BaseEngine):
    def get_required_channels(self) -> list[str]:
        return ["kline:15m"]

    async def scan_loop(self) -> None:
        pass

    def _get_warmup_symbols(self) -> list[str]:
        return [s for s in ["BTCUSDT", "ETHUSDT"] if not self._is_blacklisted(s)]


@pytest.fixture
def engine():
    config = MagicMock(spec=BaseConfig)
    config.ALPHA_ID = "test-alpha"
    config.REDIS_URL = "redis://localhost:6379"
    config.REDIS_STREAM = "paper-signals"
    config.LOG_LEVEL = "INFO"
    config.LOG_DIR = "/tmp/test_logs"
    config.DATA_MAX_CANDLES = 1000
    config.WARMUP_BARS = 50
    config.SYMBOL_BLACKLIST = ""
    config.TF = "15m"
    return MockEngine(config)


@pytest.fixture
def engine_with_blacklist():
    config = MagicMock(spec=BaseConfig)
    config.ALPHA_ID = "test-alpha"
    config.REDIS_URL = "redis://localhost:6379"
    config.REDIS_STREAM = "paper-signals"
    config.LOG_LEVEL = "INFO"
    config.LOG_DIR = "/tmp/test_logs"
    config.DATA_MAX_CANDLES = 1000
    config.WARMUP_BARS = 50
    config.SYMBOL_BLACKLIST = "BTCUSDT,DOGEUSDT"
    config.TF = "15m"
    return MockEngine(config)


def test_engine_symbol_data_is_multi_tf(engine):
    assert isinstance(engine.symbol_data, dict)


def test_engine_on_kline_message_appends_to_tf(engine):
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67500.0,
            "low": 66800.0,
            "close": 67200.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": False,
        }
    )
    sd = engine.symbol_data["BTCUSDT"]["15m"]
    assert sd.price_list[-1] == 67200.0
    assert sd.high_list[-1] == 67500.0


def test_engine_on_kline_message_separates_tfs(engine):
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67500.0,
            "low": 66800.0,
            "close": 67200.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": False,
        }
    )
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "1h",
            "open": 67000.0,
            "high": 68000.0,
            "low": 66500.0,
            "close": 67500.0,
            "volume": 500.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": False,
        }
    )
    sd_15m = engine.symbol_data["BTCUSDT"]["15m"]
    sd_1h = engine.symbol_data["BTCUSDT"]["1h"]
    assert sd_15m.price_list[-1] == 67200.0
    assert sd_1h.price_list[-1] == 67500.0
    assert sd_1h.volume_list[-1] == 500.0


def test_engine_on_kline_message_correction_overwrites(engine):
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67500.0,
            "low": 66800.0,
            "close": 67200.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": False,
        }
    )
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67600.0,
            "low": 66800.0,
            "close": 67300.0,
            "volume": 110.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": True,
        }
    )
    sd = engine.symbol_data["BTCUSDT"]["15m"]
    assert len(sd.time_list) == 1
    assert sd.high_list[-1] == 67600.0
    assert sd.volume_list[-1] == 110.0


def test_engine_on_kline_multiple_candles(engine):
    base_ts = 1716768000000
    for i in range(3):
        engine.on_kline_message(
            {
                "symbol": "ETHUSDT",
                "tf": "15m",
                "open": 3000.0 + i,
                "high": 3050.0,
                "low": 2990.0,
                "close": 3020.0 + i,
                "volume": 100.0,
                "open_time": base_ts + i * 900000,
                "close_time": base_ts + i * 900000 + 899999,
                "confirmed": True,
                "correction": False,
            }
        )
    assert len(engine.symbol_data["ETHUSDT"]["15m"].time_list) == 3


def test_engine_on_kline_ignores_missing_symbol(engine):
    engine.on_kline_message({"tf": "15m", "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "volume": 50.0, "open_time": 1716768000000})
    assert len(engine.symbol_data) == 0


def test_engine_on_kline_ignores_missing_tf(engine):
    engine.on_kline_message({"symbol": "BTCUSDT", "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "volume": 50.0, "open_time": 1716768000000})
    assert len(engine.symbol_data) == 0


def test_engine_blacklist_skips_on_kline(engine_with_blacklist):
    engine_with_blacklist.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67500.0,
            "low": 66800.0,
            "close": 67200.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "confirmed": True,
            "correction": False,
        }
    )
    assert "BTCUSDT" not in engine_with_blacklist.symbol_data


def test_engine_blacklist_allows_non_blacklisted(engine_with_blacklist):
    engine_with_blacklist.on_kline_message(
        {
            "symbol": "ETHUSDT",
            "tf": "15m",
            "open": 3000.0,
            "high": 3050.0,
            "low": 2990.0,
            "close": 3020.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "confirmed": True,
            "correction": False,
        }
    )
    assert "ETHUSDT" in engine_with_blacklist.symbol_data


def test_engine_is_blacklisted(engine):
    assert not engine._is_blacklisted("BTCUSDT")
    assert not engine._is_blacklisted("ETHUSDT")


def test_engine_is_blacklisted_with_blacklist(engine_with_blacklist):
    assert engine_with_blacklist._is_blacklisted("BTCUSDT")
    assert engine_with_blacklist._is_blacklisted("DOGEUSDT")
    assert not engine_with_blacklist._is_blacklisted("ETHUSDT")


def test_engine_load_warmup_candles(engine):
    data = {
        "symbol": "BTCUSDT",
        "tf": "15m",
        "candles": json.dumps([
            {"open_time": 1716768000000, "open": 67000.0, "high": 67500.0, "low": 66800.0, "close": 67200.0, "volume": 100.0},
            {"open_time": 1716768900000, "open": 67200.0, "high": 67800.0, "low": 67100.0, "close": 67500.0, "volume": 120.0},
        ]),
    }
    engine._load_warmup_candles(data)
    sd = engine.symbol_data["BTCUSDT"]["15m"]
    assert len(sd.time_list) == 2
    assert sd.price_list[0] == 67200.0
    assert sd.price_list[1] == 67500.0


def test_engine_load_warmup_candles_skips_blacklisted(engine_with_blacklist):
    data = {
        "symbol": "BTCUSDT",
        "tf": "15m",
        "candles": json.dumps([
            {"open_time": 1716768000000, "open": 67000.0, "high": 67500.0, "low": 66800.0, "close": 67200.0, "volume": 100.0},
        ]),
    }
    engine_with_blacklist._load_warmup_candles(data)
    assert "BTCUSDT" not in engine_with_blacklist.symbol_data


def test_engine_get_warmup_symbols_subtracts_blacklist(engine_with_blacklist):
    symbols = engine_with_blacklist._get_warmup_symbols()
    assert "BTCUSDT" not in symbols
    assert "ETHUSDT" in symbols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -m pytest alphas/base/tests/test_engine.py -v`
Expected: FAIL — `test_engine_on_kline_message_appends_to_tf` fails because `symbol_data["BTCUSDT"]` is a `SymbolData` not a `dict`

- [ ] **Step 3: Rewrite `alphas/base/engine.py`**

```python
# alphas/base/engine.py
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
        """Return symbols to request warmup data for."""

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
        if isinstance(candles_raw, str):
            candles = json.loads(candles_raw)
        else:
            candles = candles_raw

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

        tf = self.config.TF
        bars = self.config.WARMUP_BARS

        redis_client = await self._connect_redis()
        try:
            # Create consumer group BEFORE sending the request to avoid race condition
            # where MDS responds before the group exists. id="$" ensures we only receive
            # responses to THIS request, not stale messages from previous runs.
            response_stream = f"warmup:response:{self.config.ALPHA_ID}"
            try:
                redis_client.xgroup_create(response_stream, "alpha_consumer", id="$", mkstream=True)
            except redis_lib.ResponseError:
                # Group already exists — trim any stale messages so we start clean
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
            # Clean up response stream after reading to prevent accumulation across restarts
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
        logging.basicConfig(
            level=getattr(logging, self.config.LOG_LEVEL.upper(), logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(os.path.join(self.config.LOG_DIR, "bot.log")),
            ],
            force=True,
        )

        signal_push.init(self.config.REDIS_URL, self.config.REDIS_STREAM)

        loop = asyncio.get_running_loop()
        for signal_name in (sig.SIGTERM, sig.SIGINT):
            loop.add_signal_handler(signal_name, self.shutdown_event.set)

        self._logger.info("[%s] Starting alpha engine", self.config.ALPHA_ID)

        try:
            await self._request_warmup()
        except Exception as exc:
            self._logger.warning("[%s] Warmup failed: %s", self.config.ALPHA_ID, exc)

        sub_task = await self.subscribe_data_feeds()

        timeout_sec = float(getattr(self.config, "INITIAL_DATA_TIMEOUT_SEC", 30.0))
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while len(self.symbol_data) == 0 and not self.shutdown_event.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(1)

        if len(self.symbol_data) == 0:
            self._logger.warning(
                "[%s] No market data after %.1fs; starting scan loop and waiting for live data.",
                self.config.ALPHA_ID,
                timeout_sec,
            )
        else:
            total_candles = sum(
                len(tf_map.get(self.config.TF, SymbolData()).price_list)
                for tf_map in self.symbol_data.values()
            )
            self._logger.info(
                "[%s] Ready: %d symbols, %d candles at %s",
                self.config.ALPHA_ID,
                len(self.symbol_data),
                total_candles,
                self.config.TF,
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -m pytest alphas/base/tests/test_engine.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add alphas/base/engine.py alphas/base/tests/test_engine.py
git commit -m "feat: multi-TF symbol_data, blacklist, warmup request, remove _load_snapshots"
```

---

### Task 4: Update ADX engine for multi-TF and warmup

**Files:**
- Modify: `alphas/adx-trend-follow/app/engine.py`

- [ ] **Step 1: Update ADX engine**

Change `_manage_positions` and `_scan_new_signals` to use `self.symbol_data.get(symbol, {}).get(self.config.TF)`, and add `_get_warmup_symbols`:

```python
# alphas/adx-trend-follow/app/engine.py
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import compute_adx, get_candle_seconds, strategy_filter_signal
from base.engine import BaseEngine
from base.symbol_utils import get_binance_perp_symbols

logger = logging.getLogger(__name__)


class ADXTrendFollowEngine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}

    def get_required_channels(self) -> list[str]:
        return [f"kline:{self.config.TF}"]

    def _get_warmup_symbols(self) -> list[str]:
        all_symbols = get_binance_perp_symbols()
        return [s for s in all_symbols if not self._is_blacklisted(s)]

    async def scan_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self._wait_until_next_candle_offset()
                if self.shutdown_event.is_set():
                    break

                await self._manage_positions()
                await self._scan_new_signals()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scan error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    async def _wait_until_next_candle_offset(self) -> None:
        candle_len = get_candle_seconds(self.config.TF)
        now = time.time()
        next_candle = (int(now // candle_len) + 1) * candle_len
        target = next_candle + self.config.OFFSET_CANDLE_SEC
        wait = target - now
        if wait > 0:
            await asyncio.sleep(wait)

    async def _manage_positions(self) -> None:
        if not self._open_positions:
            return

        snapshots = {}
        async with self.data_lock:
            for symbol, pos in self._open_positions.items():
                sd = self.symbol_data.get(symbol, {}).get(self.config.TF)
                if sd and sd.price_list and sd.low_list and sd.high_list:
                    snapshots[symbol] = {
                        "close": sd.price_list[-1],
                        "low": sd.low_list[-1],
                        "high": sd.high_list[-1],
                        "pos": dict(pos),
                    }

        to_close: list[dict] = []
        to_modify: list[dict] = []
        to_remove: list[str] = []

        for symbol, snap in snapshots.items():
            close = snap["close"]
            low = snap["low"]
            high = snap["high"]
            pos = snap["pos"]
            side = pos["side"]
            entry = pos["entry"]
            current_sl = pos["sl"]
            current_tp = pos["tp"]
            position_id = pos["position_id"]
            bar_count = pos["bar_count"]
            be_activated = pos["be_activated"]

            sl_hit = (side == "LONG" and low <= current_sl) or (side == "SHORT" and high >= current_sl)
            if sl_hit:
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": current_sl,
                        "reason": "SL_HIT",
                    }
                )
                to_remove.append(symbol)
                continue

            tp_hit = (side == "LONG" and high >= current_tp) or (side == "SHORT" and low <= current_tp)
            if tp_hit:
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": current_tp,
                        "reason": "TP_CAP",
                    }
                )
                to_remove.append(symbol)
                continue

            new_bar_count = bar_count + 1
            if new_bar_count >= self.config.MAX_HOLD_CANDLES:
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": close,
                        "reason": "MAX_HOLD",
                    }
                )
                to_remove.append(symbol)
                continue

            new_sl = current_sl
            new_be = be_activated

            if side == "LONG":
                if not be_activated and close >= entry * (1 + self.config.BE_TRIGGER_PCT):
                    new_sl = max(new_sl, entry)
                    new_be = True
                if new_be:
                    new_sl = max(new_sl, close * (1 - self.config.TRAIL_DIST_PCT))
            else:
                if not be_activated and close <= entry * (1 - self.config.BE_TRIGGER_PCT):
                    new_sl = min(new_sl, entry)
                    new_be = True
                if new_be:
                    new_sl = min(new_sl, close * (1 + self.config.TRAIL_DIST_PCT))

            self._open_positions[symbol]["bar_count"] = new_bar_count
            self._open_positions[symbol]["be_activated"] = new_be

            if new_sl != current_sl:
                self._open_positions[symbol]["sl"] = new_sl
                to_modify.append({"position_id": position_id, "sl": new_sl})

        for item in to_modify:
            self.push_signal("MODIFY", position_id=item["position_id"], sl=item["sl"])
            logger.debug("[MODIFY] position=%s new_sl=%.6f", item["position_id"], item["sl"])

        for item in to_close:
            self.push_signal(
                "CLOSE",
                position_id=item["position_id"],
                exit_price=item["exit_price"],
                reason=item["reason"],
            )
            logger.info("[CLOSE] %s reason=%s @ %s", item["symbol"], item["reason"], item["exit_price"])

        for symbol in to_remove:
            self._open_positions.pop(symbol, None)

    async def _scan_new_signals(self) -> None:
        if len(self._open_positions) >= self.config.MAX_CONCURRENT_POSITIONS:
            return

        snapshot_rows = []
        async with self.data_lock:
            btc_sd = self.symbol_data.get("BTCUSDT", {}).get(self.config.TF)
            if btc_sd is None or len(btc_sd.price_list) < settings.ADX_PERIOD * 2:
                return

            btc_pl = list(btc_sd.price_list)
            btc_hl = list(btc_sd.high_list)
            btc_ll = list(btc_sd.low_list)

            adx_btc = compute_adx(btc_hl, btc_ll, btc_pl, settings.ADX_PERIOD)
            if adx_btc < settings.ADX_THRESHOLD:
                return

            for symbol, tf_map in self.symbol_data.items():
                if symbol == "BTCUSDT" or symbol in self._open_positions:
                    continue
                sd = tf_map.get(self.config.TF)
                if not sd or not sd.price_list or not sd.volume_list:
                    continue
                snapshot_rows.append(
                    {
                        "symbol": symbol,
                        "price_list": list(sd.price_list),
                        "volume_list": list(sd.volume_list),
                        "high_list": list(sd.high_list),
                        "low_list": list(sd.low_list),
                    }
                )

        signals = []
        for row in snapshot_rows:
            signal = strategy_filter_signal(
                symbol=row["symbol"],
                price_list=row["price_list"],
                volume_list=row["volume_list"],
                high_list=row["high_list"],
                low_list=row["low_list"],
                btc_price_list=btc_pl,
                btc_high_list=btc_hl,
                btc_low_list=btc_ll,
            )
            if signal:
                signals.append(signal)

        signals.sort(key=lambda item: item.get("vol_spike", 0), reverse=True)
        available_slots = self.config.MAX_CONCURRENT_POSITIONS - len(self._open_positions)

        for signal in signals[:available_slots]:
            symbol = signal["symbol"]
            if symbol in self._open_positions:
                continue

            side = signal["recommend"]
            entry = signal["entry"]
            position_id = str(uuid.uuid4())
            if side == "LONG":
                sl = entry * (1 - self.config.INITIAL_SL_PCT)
                tp = entry * (1 + self.config.TP_CAP_PCT)
            else:
                sl = entry * (1 + self.config.INITIAL_SL_PCT)
                tp = entry * (1 - self.config.TP_CAP_PCT)

            qty = self.config.INVEST_PER_TRADE * self.config.LEVERAGE / entry
            timestamp = datetime.now(timezone.utc).isoformat()

            self.push_signal(
                "OPEN",
                symbol=symbol,
                side=side,
                entry=entry,
                qty=qty,
                tp=tp,
                sl=sl,
                leverage=self.config.LEVERAGE,
                position_id=position_id,
                exchange=self.config.EXCHANGE,
                fee_pct=self.config.FEE_PCT,
                metadata=json.dumps(
                    {
                        "vol_spike": signal.get("vol_spike"),
                        "price_move": signal.get("price_move"),
                        "btc_adx": signal.get("btc_adx"),
                    }
                ),
                timestamp=timestamp,
            )

            self._open_positions[symbol] = {
                "position_id": position_id,
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "bar_count": 0,
                "be_activated": False,
            }
            logger.info("[SIGNAL] OPEN %s %s @ %s sl=%.4f tp=%.4f", side, symbol, entry, sl, tp)
```

- [ ] **Step 2: Verify syntax**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -c "import sys; sys.path.insert(0, 'alphas/adx-trend-follow'); from app.engine import ADXTrendFollowEngine; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add alphas/adx-trend-follow/app/engine.py
git commit -m "feat: adx engine multi-TF symbol_data access, warmup symbols, blacklist"
```

---

### Task 5: Update Wilder engine for multi-TF and warmup

**Files:**
- Modify: `alphas/wilder/app/engine.py`

- [ ] **Step 1: Update Wilder engine**

Same pattern as ADX — change `self.symbol_data.get(symbol)` to `self.symbol_data.get(symbol, {}).get(self.config.TF)`, add `_get_warmup_symbols`, remove any `fetch_closed_candles_batch` references:

```python
# alphas/wilder/app/engine.py
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import get_candle_seconds, wilder_filter_signal
from base.engine import BaseEngine
from base.models import SymbolData
from base.symbol_utils import get_top_n_binance_perps

logger = logging.getLogger(__name__)


class WilderEngine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}

    def get_required_channels(self) -> list[str]:
        return [f"kline:{self.config.TF}"]

    def _get_warmup_symbols(self) -> list[str]:
        all_symbols = get_top_n_binance_perps(50)
        return [s for s in all_symbols if not self._is_blacklisted(s)]

    async def scan_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self._wait_until_next_candle_offset()
                if self.shutdown_event.is_set():
                    break

                await self._manage_positions()
                await self._scan_new_signals()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scan error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    async def _wait_until_next_candle_offset(self) -> None:
        candle_len = get_candle_seconds(self.config.TF)
        now = time.time()
        next_candle = (int(now // candle_len) + 1) * candle_len
        target = next_candle + self.config.OFFSET_CANDLE_SEC
        wait = target - now
        if wait > 0:
            await asyncio.sleep(wait)

    def _compute_current_atr(self, sd: SymbolData) -> float:
        period = settings.ATR_PERIOD
        if len(sd.price_list) < period:
            return 0.0
        df = pd.DataFrame(
            {
                "high": sd.high_list[-period * 3 :],
                "low": sd.low_list[-period * 3 :],
                "close": sd.price_list[-period * 3 :],
            }
        )
        atr_series = ta.atr(high=df["high"], low=df["low"], close=df["close"], length=period)
        if atr_series is None or atr_series.empty:
            return 0.0
        value = atr_series.iloc[-1]
        return float(value) if pd.notna(value) else 0.0

    async def _manage_positions(self) -> None:
        if not self._open_positions:
            return

        snapshots = {}
        async with self.data_lock:
            for symbol, pos in self._open_positions.items():
                sd = self.symbol_data.get(symbol, {}).get(self.config.TF)
                if sd and sd.price_list and sd.low_list and sd.high_list:
                    snapshots[symbol] = {
                        "close": sd.price_list[-1],
                        "low": sd.low_list[-1],
                        "high": sd.high_list[-1],
                        "atr": self._compute_current_atr(sd),
                        "pos": dict(pos),
                    }

        to_close: list[dict] = []
        to_modify: list[dict] = []
        to_remove: list[str] = []

        for symbol, snap in snapshots.items():
            close = snap["close"]
            low = snap["low"]
            high = snap["high"]
            atr = snap["atr"]
            pos = snap["pos"]
            side = pos["side"]
            current_sl = pos["sl"]
            current_tp = pos["tp"]
            position_id = pos["position_id"]

            sl_hit = (side == "LONG" and low <= current_sl) or (side == "SHORT" and high >= current_sl)
            if sl_hit:
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": current_sl,
                        "reason": "SL_HIT",
                    }
                )
                to_remove.append(symbol)
                continue

            tp_hit = (side == "LONG" and high >= current_tp) or (side == "SHORT" and low <= current_tp)
            if tp_hit:
                to_close.append(
                    {
                        "symbol": symbol,
                        "position_id": position_id,
                        "exit_price": current_tp,
                        "reason": "TP_HIT",
                    }
                )
                to_remove.append(symbol)
                continue

            if atr <= 0:
                continue

            new_sl = current_sl
            if side == "LONG":
                new_sl = max(new_sl, close - settings.TRAIL_ATR_MULT * atr)
            else:
                new_sl = min(new_sl, close + settings.TRAIL_ATR_MULT * atr)

            if new_sl != current_sl:
                self._open_positions[symbol]["sl"] = new_sl
                to_modify.append({"position_id": position_id, "sl": new_sl})

        for item in to_modify:
            self.push_signal("MODIFY", position_id=item["position_id"], sl=item["sl"])
            logger.debug("[MODIFY] position=%s new_sl=%.6f", item["position_id"], item["sl"])

        for item in to_close:
            self.push_signal(
                "CLOSE",
                position_id=item["position_id"],
                exit_price=item["exit_price"],
                reason=item["reason"],
            )
            logger.info("[CLOSE] %s reason=%s @ %s", item["symbol"], item["reason"], item["exit_price"])

        for symbol in to_remove:
            self._open_positions.pop(symbol, None)

    async def _scan_new_signals(self) -> None:
        if len(self._open_positions) >= self.config.MAX_CONCURRENT_POSITIONS:
            return

        snapshot_rows = []
        async with self.data_lock:
            for symbol, tf_map in self.symbol_data.items():
                if symbol in self._open_positions:
                    continue
                sd = tf_map.get(self.config.TF)
                if not sd or not sd.price_list or not sd.high_list or not sd.low_list:
                    continue
                snapshot_rows.append(
                    {
                        "symbol": symbol,
                        "price_list": list(sd.price_list),
                        "high_list": list(sd.high_list),
                        "low_list": list(sd.low_list),
                    }
                )

        signals = []
        for row in snapshot_rows:
            signal = wilder_filter_signal(
                symbol=row["symbol"],
                price_list=row["price_list"],
                high_list=row["high_list"],
                low_list=row["low_list"],
            )
            if signal:
                signals.append(signal)

        available_slots = self.config.MAX_CONCURRENT_POSITIONS - len(self._open_positions)
        for signal in signals[:available_slots]:
            symbol = signal["symbol"]
            if symbol in self._open_positions:
                continue

            side = signal["recommend"]
            entry = signal["entry"]
            sl = signal["sl"]
            tp = signal["tp"]
            atr = signal["atr"]
            position_id = str(uuid.uuid4())
            quantity = self.config.INVEST_PER_TRADE * self.config.LEVERAGE / entry
            timestamp = datetime.now(timezone.utc).isoformat()

            self.push_signal(
                "OPEN",
                symbol=symbol,
                side=side,
                entry=entry,
                qty=quantity,
                tp=tp,
                sl=sl,
                leverage=self.config.LEVERAGE,
                position_id=position_id,
                exchange=self.config.EXCHANGE,
                fee_pct=self.config.FEE_PCT,
                metadata=json.dumps(
                    {
                        "regime": signal.get("regime"),
                        "adx": signal.get("adx"),
                        "plus_di": signal.get("plus_di"),
                        "minus_di": signal.get("minus_di"),
                        "rsi_curr": signal.get("rsi_curr"),
                        "atr": atr,
                    }
                ),
                timestamp=timestamp,
            )

            self._open_positions[symbol] = {
                "position_id": position_id,
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp": tp,
            }
            logger.info(
                "[SIGNAL] OPEN %s %s @ %.4f sl=%.4f tp=%.4f atr=%.4f regime=%s",
                side,
                symbol,
                entry,
                sl,
                tp,
                atr,
                signal.get("regime"),
            )
```

- [ ] **Step 2: Verify syntax**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -c "import sys; sys.path.insert(0, 'alphas/wilder'); from app.engine import WilderEngine; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add alphas/wilder/app/engine.py
git commit -m "feat: wilder engine multi-TF symbol_data access, warmup symbols, blacklist"
```

---

### Task 6: Create MDS warmup handler

**Files:**
- Modify: `market-data-service/app/aggregator.py` (add `get_candles()`)
- Create: `market-data-service/app/warmup_handler.py`
- Create: `market-data-service/tests/test_warmup_handler.py`

- [ ] **Step 0: Add `get_candles()` to Aggregator**

`WarmupHandler` needs a public method to retrieve candle lists from the aggregator. Add this method to `app/aggregator.py`:

```python
def get_candles(self, symbol: str, tf: str) -> list[KlineCandle]:
    """Return stored candles for symbol+tf; empty list if not found."""
    if tf == "1m":
        return list(self._1m_candles.get(symbol, []))
    return list(self._tf_candles.get(symbol, {}).get(tf, []))
```

- [ ] **Step 1: Write failing test**

```python
# market-data-service/tests/test_warmup_handler.py
import json
from unittest.mock import MagicMock, patch

import pytest

from app.aggregator import Aggregator
from app.models import KlineCandle
from app.warmup_handler import WarmupHandler


@pytest.fixture
def aggregator():
    agg = Aggregator(timeframes=["15m", "1h"], max_1m_per_symbol=100)
    base_ts = 1716768000000
    for i in range(60):
        candle = KlineCandle(
            symbol="BTCUSDT",
            tf="1m",
            open=67000.0 + i,
            high=67500.0 + i,
            low=66800.0 + i,
            close=67200.0 + i,
            volume=100.0,
            open_time=base_ts + i * 60000,
            close_time=base_ts + i * 60000 + 59999,
            confirmed=True,
        )
        agg.on_1m_close(candle)
    return agg


@pytest.fixture
def redis_mock():
    r = MagicMock()
    r.xadd = MagicMock()
    return r


def test_warmup_handler_process_request(aggregator, redis_mock):
    handler = WarmupHandler(aggregator, redis_mock)
    request = {
        "alpha_id": "test-alpha",
        "tf": "1h",
        "bars": "10",
        "symbols": "BTCUSDT",
    }
    handler._process_request(request)

    redis_mock.xadd.assert_called_once()
    call_args = redis_mock.xadd.call_args
    stream_name = call_args[0][0]
    assert stream_name == "warmup:response:test-alpha"

    fields = call_args[0][1]
    assert fields["symbol"] == "BTCUSDT"
    assert fields["tf"] == "1h"
    candles = json.loads(fields["candles"])
    assert len(candles) == 1
    assert candles[0]["symbol"] == "BTCUSDT"
    assert candles[0]["tf"] == "1h"


def test_warmup_handler_process_request_unknown_symbol(aggregator, redis_mock):
    handler = WarmupHandler(aggregator, redis_mock)
    request = {
        "alpha_id": "test-alpha",
        "tf": "1h",
        "bars": "10",
        "symbols": "ETHUSDT",
    }
    handler._process_request(request)
    redis_mock.xadd.assert_called_once()
    fields = redis_mock.xadd.call_args[0][1]
    assert fields["symbol"] == "ETHUSDT"
    candles = json.loads(fields["candles"])
    assert len(candles) == 0


def test_warmup_handler_process_request_limited_bars(aggregator, redis_mock):
    handler = WarmupHandler(aggregator, redis_mock)
    request = {
        "alpha_id": "test-alpha",
        "tf": "1h",
        "bars": "1",
        "symbols": "BTCUSDT",
    }
    handler._process_request(request)
    candles = json.loads(redis_mock.xadd.call_args[0][1]["candles"])
    assert len(candles) <= 1


def test_warmup_handler_process_multiple_symbols(aggregator, redis_mock):
    base_ts = 1716768000000
    for i in range(60):
        candle = KlineCandle(
            symbol="ETHUSDT",
            tf="1m",
            open=3000.0 + i,
            high=3050.0 + i,
            low=2990.0 + i,
            close=3020.0 + i,
            volume=100.0,
            open_time=base_ts + i * 60000,
            close_time=base_ts + i * 60000 + 59999,
            confirmed=True,
        )
        aggregator.on_1m_close(candle)

    handler = WarmupHandler(aggregator, redis_mock)
    request = {
        "alpha_id": "test-alpha",
        "tf": "1h",
        "bars": "5",
        "symbols": "BTCUSDT,ETHUSDT",
    }
    handler._process_request(request)
    assert redis_mock.xadd.call_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -m pytest market-data-service/tests/test_warmup_handler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.warmup_handler'`

- [ ] **Step 3: Write implementation**

```python
# market-data-service/app/warmup_handler.py
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging

import redis as redis_lib

from app.aggregator import Aggregator

logger = logging.getLogger(__name__)


class WarmupHandler:
    def __init__(self, aggregator: Aggregator, redis_client: redis_lib.Redis):
        self.aggregator = aggregator
        self.redis = redis_client
        self._shutdown = asyncio.Event()
        self._group_name = "mds_warmup"
        self._consumer_name = "mds-warmup-1"

    def _process_request(self, request: dict) -> None:
        alpha_id = request.get("alpha_id", "")
        tf = request.get("tf", "")
        bars = int(request.get("bars", "0"))
        symbols_str = request.get("symbols", "")
        if not alpha_id or not tf or not symbols_str:
            return

        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
        response_stream = f"warmup:response:{alpha_id}"

        for symbol in symbols:
            candles = self.aggregator.get_candles(symbol, tf)
            if bars > 0:
                candles = candles[-bars:]

            candle_dicts = [dataclasses.asdict(c) for c in candles]
            self.redis.xadd(
                response_stream,
                {
                    "symbol": symbol,
                    "tf": tf,
                    "candles": json.dumps(candle_dicts),
                },
            )

        logger.info(
            "[WARMUP] Responded to %s: %d symbols at %s, %d bars requested",
            alpha_id,
            len(symbols),
            tf,
            bars,
        )

    async def run(self) -> None:
        stream = "warmup:request"
        try:
            self.redis.xgroup_create(stream, self._group_name, id="0", mkstream=True)
        except redis_lib.ResponseError:
            pass

        while not self._shutdown.is_set():
            try:
                messages = await asyncio.to_thread(
                    self.redis.xreadgroup,
                    self._group_name,
                    self._consumer_name,
                    {stream: ">"},
                    count=10,
                    block=5000,
                )

                if not messages:
                    continue

                for _stream, entries in messages:
                    for msg_id, fields in entries:
                        self._process_request(fields)
                        self.redis.xack(stream, self._group_name, msg_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[WARMUP] Error: %s", exc)
                await asyncio.sleep(1)

    def shutdown(self) -> None:
        self._shutdown.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -m pytest market-data-service/tests/test_warmup_handler.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add market-data-service/app/warmup_handler.py market-data-service/tests/test_warmup_handler.py
git commit -m "feat: add MDS warmup handler for Redis Stream request-reply"
```

---

### Task 7: Integrate warmup handler into MDS main.py

**Files:**
- Modify: `market-data-service/app/main.py`

- [ ] **Step 1: Add warmup handler import and task**

Add import at top of `main.py`:

```python
from app.warmup_handler import WarmupHandler
```

Add warmup handler creation and task in `run_service()`, after the initial data load (after the snapshot population block, before the WS batch tasks):

```python
    warmup_handler = WarmupHandler(aggregator, redis_client)
    tasks.append(asyncio.create_task(warmup_handler.run()))
```

Add `warmup_handler.shutdown()` in the finally block alongside `kline_feed.shutdown()`, `ticker_feed.shutdown()`, `reconciler.shutdown()`.

The full updated `run_service` function:

```python
async def run_service() -> None:
    configure_logging()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    for signal_name in (sig.SIGTERM, sig.SIGINT):
        loop.add_signal_handler(signal_name, shutdown_event.set)

    symbols = get_symbol_universe()
    logger.info("Symbol universe: %d symbols", len(symbols))

    aggregator = Aggregator(timeframes=settings.get_timeframes(), max_1m_per_symbol=settings.HISTORY_CANDLES)
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
    )

    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(_health_loop(shutdown_event)))

    logger.info("Loading initial 1m data")
    await kline_feed.load_initial_data(
        client,
        symbols,
        store_size=settings.HISTORY_CANDLES,
        semaphore_limit=settings.REST_SEMAPHORE,
    )

    logger.info("Populating Redis snapshots")
    snapshot_count = 0
    for symbol, candles in aggregator._1m_candles.items():
        for candle in candles[-settings.SNAPSHOT_MAX_CANDLES:]:
            publisher.publish_kline_snapshot(candle, max_candles=settings.SNAPSHOT_MAX_CANDLES)
            snapshot_count += 1
    for symbol, tf_map in aggregator._tf_candles.items():
        for candles in tf_map.values():
            for candle in candles[-settings.SNAPSHOT_MAX_CANDLES:]:
                publisher.publish_kline_snapshot(candle, max_candles=settings.SNAPSHOT_MAX_CANDLES)
                snapshot_count += 1
    logger.info("Snapshots written: %d candles", snapshot_count)

    warmup_handler = WarmupHandler(aggregator, redis_client)
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
```

- [ ] **Step 2: Verify syntax**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -c "from market_data_service.app.main import run_service; print('OK')" 2>/dev/null || python -c "import ast; ast.parse(open('market-data-service/app/main.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Step 3: Commit**

```bash
git add market-data-service/app/main.py
git commit -m "feat: integrate warmup handler into MDS main loop"
```

---

### Task 8: Update Dockerfiles to include symbol_utils.py

**Files:**
- Modify: `alphas/adx-trend-follow/Dockerfile`
- Modify: `alphas/wilder/Dockerfile`

Both Dockerfiles already `COPY base/ ./base/` which will include `symbol_utils.py`. However, verify the Dockerfile `context` is set to `..` (parent of alpha dir) so the `base/` copy works.

- [ ] **Step 1: Verify Dockerfiles**

Both existing Dockerfiles already have `COPY base/ ./base/` and `context: ..` in docker-compose.yml. No changes needed — `symbol_utils.py` will be included automatically.

- [ ] **Step 2: Commit (if any changes made)**

Only commit if files were actually modified.

---

### Task 9: Run all existing tests to verify no regressions

**Files:**
- No changes

- [ ] **Step 1: Run base engine tests**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -m pytest alphas/base/tests/ -v`
Expected: All PASS

- [ ] **Step 2: Run MDS tests**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -m pytest market-data-service/tests/ -v`
Expected: All PASS

- [ ] **Step 3: Run worker tests**

Run: `cd /home/namvt/Desktop/quant-space/system/paper-trade-system && python -m pytest worker/tests/ -v`
Expected: All PASS

---

## Self-Review Checklist

**1. Spec coverage:**

| Spec section | Task |
|---|---|
| Symbol blacklist (BaseConfig, _is_blacklisted, filtering) | Task 2, Task 3 |
| Warmup request (XADD warmup:request) | Task 3 (_request_warmup) |
| Warmup response (XADD warmup:response:{alpha_id}) | Task 6 |
| Multi-TF symbol_data structure | Task 3 |
| on_kline_message fix (use tf field) | Task 3 |
| _load_warmup_candles | Task 3 |
| _get_warmup_symbols (abstract) | Task 3 |
| ADX: _get_warmup_symbols, multi-TF access | Task 4 |
| Wilder: _get_warmup_symbols, multi-TF access | Task 5 |
| MDS warmup handler module | Task 6 |
| MDS aggregator.get_candles() | Task 6 (Step 0) |
| MDS main.py integration | Task 7 |
| Remove _load_snapshots | Task 3 |
| symbol_utils.py (moved helpers) | Task 1 |
| Dockerfiles | Task 8 (no-op, verified) |

**2. Placeholder scan:** No TBD, TODO, or "implement later" found.

**3. Type consistency:**
- `symbol_data: dict[str, dict[str, SymbolData]]` — consistent across BaseEngine, ADX, Wilder
- `_is_blacklisted(symbol: str) -> bool` — consistent
- `_get_warmup_symbols() -> list[str]` — consistent across BaseEngine (abstract), ADX, Wilder; MockEngine filters blacklist
- `_load_warmup_candles(data: dict) -> None` — consistent
- Warmup response fields: `{symbol, tf, candles}` — consistent between handler (`dataclasses.asdict`) and engine (xreadgroup)
- Config field names: `WARMUP_BARS`, `SYMBOL_BLACKLIST`, `TF` — consistent everywhere
- Race condition fix: `xgroup_create(id="$")` before `xadd` ensures no stale messages delivered
- Stream cleanup: `redis_client.delete(response_stream)` after warmup prevents accumulation across restarts
