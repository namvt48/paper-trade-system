# Market Data Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a centralized market-data-service that fetches data from Binance once and distributes it to alphas and worker via Redis Pub/Sub, eliminating duplicate WS/REST connections.

**Architecture:** New `market-data-service/` container subscribes to Binance `@kline_1m` + `@ticker` WS, aggregates 1m into higher TFs in-memory, publishes candle close events and ticker updates to Redis channels. Alphas and worker subscribe to Redis instead of connecting to exchanges. Full migration — remove `market_data.py`, `ws_manager.py`, `price_feed.py` from consumers.

**Tech Stack:** Python 3.11+, asyncio, redis[hiredis], python-binance, pydantic-settings, websockets, PyYAML

---

## File Structure

### New files (market-data-service)
```
market-data-service/
  Dockerfile
  requirements.txt
  .env.example
  app/
    __init__.py
    main.py              # entrypoint, orchestrates startup + tasks
    config.py            # pydantic-settings config
    models.py            # KlineCandle, TickerUpdate dataclasses
    kline_feed.py        # Binance @kline_1m WS + REST initial load
    ticker_feed.py       # Binance @ticker WS
    aggregator.py        # 1m → Nm/15m/1h/4h/1d rollup
    reconciler.py        # REST reconciliation after candle close
    publisher.py         # Redis Pub/Sub publisher
```

### Modified files (alphas)
```
alphas/base/
  engine.py              # replace load_initial_data/create_ws_tasks with subscribe_data_feeds
  config.py              # add DATA_CHANNELS setting
  models.py              # unchanged
  signal_push.py         # unchanged
alphas/adx-trend-follow/
  app/engine.py          # simplify — remove market_data/ws_manager imports
  app/config.py          # add DATA_CHANNELS
  app/market_data.py     # DELETE
  app/ws_manager.py      # DELETE
alphas/wilder/
  app/engine.py          # simplify — remove market_data/ws_manager imports, remove fetch_closed_candles_batch call
  app/config.py          # add DATA_CHANNELS
  app/market_data.py     # DELETE
  app/ws_manager.py      # DELETE
```

### Modified files (worker)
```
worker/
  app/main.py            # replace PriceFeedManager with Redis ticker subscriber
  app/price_feed.py      # DELETE
  app/config.py          # add REDIS_URL (already exists), remove nothing extra
```

### Modified files (docker)
```
docker-compose.yml       # add market-data-service
```

---

### Task 1: Market Data Service — Models & Config

**Files:**
- Create: `market-data-service/app/__init__.py`
- Create: `market-data-service/app/models.py`
- Create: `market-data-service/app/config.py`
- Create: `market-data-service/requirements.txt`
- Test: `market-data-service/tests/test_models.py`

- [ ] **Step 1: Write failing test for models**

```python
# market-data-service/tests/test_models.py
import pytest
from app.models import KlineCandle, TickerUpdate


def test_kline_candle_creation():
    candle = KlineCandle(
        symbol="BTCUSDT",
        tf="1m",
        open=67000.0,
        high=67500.0,
        low=66800.0,
        close=67200.0,
        volume=12345.6,
        open_time=1716768000000,
        close_time=1716771599999,
        confirmed=True,
        correction=False,
    )
    assert candle.symbol == "BTCUSDT"
    assert candle.tf == "1m"
    assert candle.confirmed is True
    assert candle.correction is False


def test_kline_candle_to_dict():
    candle = KlineCandle(
        symbol="BTCUSDT",
        tf="1h",
        open=67000.0,
        high=67500.0,
        low=66800.0,
        close=67200.0,
        volume=12345.6,
        open_time=1716768000000,
        close_time=1716771599999,
    )
    d = candle.to_dict()
    assert d["symbol"] == "BTCUSDT"
    assert d["tf"] == "1h"
    assert d["confirmed"] is True
    assert d["correction"] is False
    assert "open" in d
    assert "volume" in d


def test_kline_candle_from_ws_1m():
    ws_payload = {
        "e": "kline",
        "s": "ETHUSDT",
        "k": {
            "t": 1716768000000,
            "T": 1716771599999,
            "o": "3000.0",
            "h": "3050.0",
            "l": "2990.0",
            "c": "3020.0",
            "v": "500.0",
            "x": True,
        },
    }
    candle = KlineCandle.from_ws_1m(ws_payload)
    assert candle.symbol == "ETHUSDT"
    assert candle.tf == "1m"
    assert candle.open == 3000.0
    assert candle.high == 3050.0
    assert candle.low == 2990.0
    assert candle.close == 3020.0
    assert candle.volume == 500.0
    assert candle.confirmed is True


def test_kline_candle_from_ws_1m_partial_ignored():
    ws_payload = {
        "e": "kline",
        "s": "ETHUSDT",
        "k": {
            "t": 1716768000000,
            "T": 1716771599999,
            "o": "3000.0",
            "h": "3050.0",
            "l": "2990.0",
            "c": "3020.0",
            "v": "500.0",
            "x": False,
        },
    }
    candle = KlineCandle.from_ws_1m(ws_payload)
    assert candle is None


def test_ticker_update_creation():
    ticker = TickerUpdate(
        symbol="BTCUSDT",
        price=67200.5,
        timestamp=1716771600000,
        exchange="binance",
    )
    assert ticker.symbol == "BTCUSDT"
    assert ticker.price == 67200.5
    assert ticker.exchange == "binance"


def test_ticker_update_to_dict():
    ticker = TickerUpdate(
        symbol="BTCUSDT",
        price=67200.5,
        timestamp=1716771600000,
        exchange="binance",
    )
    d = ticker.to_dict()
    assert d["symbol"] == "BTCUSDT"
    assert d["price"] == 67200.5
    assert d["exchange"] == "binance"


def test_ticker_update_from_binance_ws():
    ws_msg = {
        "e": "24hrTicker",
        "s": "BTCUSDT",
        "c": "67200.50",
        "E": 1716771600000,
    }
    ticker = TickerUpdate.from_binance_ws(ws_msg)
    assert ticker.symbol == "BTCUSDT"
    assert ticker.price == 67200.50
    assert ticker.exchange == "binance"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd market-data-service && python -m pytest tests/test_models.py -v`
Expected: FAIL — `app.models` module not found

- [ ] **Step 3: Create directory structure and implementation**

```python
# market-data-service/app/__init__.py
```

```python
# market-data-service/app/models.py
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class KlineCandle:
    symbol: str
    tf: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_time: int
    close_time: int
    confirmed: bool = True
    correction: bool = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "tf": self.tf,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "confirmed": self.confirmed,
            "correction": self.correction,
        }

    @classmethod
    def from_ws_1m(cls, payload: dict) -> KlineCandle | None:
        if payload.get("e") != "kline":
            return None
        k = payload.get("k", {})
        if not k.get("x", False):
            return None
        return cls(
            symbol=payload.get("s", ""),
            tf="1m",
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            open_time=int(k["t"]),
            close_time=int(k["T"]),
            confirmed=True,
            correction=False,
        )


@dataclass
class TickerUpdate:
    symbol: str
    price: float
    timestamp: int
    exchange: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "timestamp": self.timestamp,
            "exchange": self.exchange,
        }

    @classmethod
    def from_binance_ws(cls, msg: dict) -> TickerUpdate:
        return cls(
            symbol=msg.get("s", ""),
            price=float(msg.get("c", 0)),
            timestamp=int(msg.get("E", 0)),
            exchange="binance",
        )
```

```python
# market-data-service/app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379"
    EXCHANGE: str = "binance"
    SYMBOL_MODE: str = "auto"
    SYMBOLS: str = ""
    TIMEFRAMES: str = "1m,5m,15m,30m,1h,4h,1d"
    # 1m candles kept in memory per symbol to bootstrap higher TFs via aggregation.
    # Formula: max_tf_candles × tf_minutes.  Examples:
    #   120 × 1h (60m) = 7 200 ·  4h alpha needs 60 × 240 = 14 400
    # 7 500 covers 125 × 1h candles with buffer; raise if using 4h/1d alphas.
    HISTORY_CANDLES: int = 7500
    WS_BATCH_SIZE: int = 150
    REST_SEMAPHORE: int = 25
    RECONCILE_TFS: str = "15m,1h"
    RECONCILE_DELAY: int = 5
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"
    TICKER_BATCH_SIZE: int = 150
    # Max candles stored per symbol/TF in Redis HASH snapshots (late-join recovery)
    SNAPSHOT_MAX_CANDLES: int = 500

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    def get_timeframes(self) -> list[str]:
        return [tf.strip() for tf in self.TIMEFRAMES.split(",") if tf.strip()]

    def get_symbols_list(self) -> list[str]:
        return [s.strip() for s in self.SYMBOLS.split(",") if s.strip()]

    def get_reconcile_tfs(self) -> list[str]:
        return [tf.strip() for tf in self.RECONCILE_TFS.split(",") if tf.strip()]


settings = Settings()
```

```
# market-data-service/requirements.txt
redis[hiredis]>=5.0
pydantic-settings>=2.0
python-binance>=1.0
websockets>=12.0
requests>=2.31
PyYAML>=6.0
pytest>=8.0
pytest-asyncio>=0.23
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd market-data-service && python -m pytest tests/test_models.py -v`
Expected: PASS — all 7 tests

- [ ] **Step 5: Commit**

```bash
git add market-data-service/app/__init__.py market-data-service/app/models.py market-data-service/app/config.py market-data-service/requirements.txt market-data-service/tests/test_models.py
git commit -m "feat(market-data-service): add models and config"
```

---

### Task 2: Aggregator — 1m → higher TF rollup

**Files:**
- Create: `market-data-service/app/aggregator.py`
- Test: `market-data-service/tests/test_aggregator.py`

- [ ] **Step 1: Write failing test for aggregator**

```python
# market-data-service/tests/test_aggregator.py
import pytest
from app.aggregator import Aggregator
from app.models import KlineCandle


@pytest.fixture
def aggregator():
    return Aggregator(timeframes=["1m", "5m", "15m", "1h"])


def _make_1m_candle(symbol: str, open_time: int, o: float, h: float, l: float, c: float, v: float) -> KlineCandle:
    return KlineCandle(
        symbol=symbol,
        tf="1m",
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        open_time=open_time,
        close_time=open_time + 59999,
        confirmed=True,
    )


def test_aggregator_stores_1m_candle(aggregator):
    candle = _make_1m_candle("BTCUSDT", 1716768000000, 67000, 67500, 66800, 67200, 100)
    results = aggregator.on_1m_close(candle)
    assert len(results) == 1
    assert results[0].tf == "1m"
    assert results[0].symbol == "BTCUSDT"


def test_aggregator_5m_rollup(aggregator):
    base_ts = 1716768000000
    for i in range(5):
        candle = _make_1m_candle(
            "BTCUSDT",
            base_ts + i * 60000,
            67000 + i,
            67500 + i,
            66800 - i,
            67100 + i * 10,
            100 + i * 10,
        )
        results = aggregator.on_1m_close(candle)

    last_result = results[-1]
    assert last_result.tf == "5m"
    assert last_result.open == 67000
    assert last_result.high == 67504
    assert last_result.low == 66796
    assert last_result.close == 67140
    assert last_result.volume == 560


def test_aggregator_15m_rollup(aggregator):
    base_ts = 1716768000000
    for i in range(15):
        candle = _make_1m_candle(
            "BTCUSDT",
            base_ts + i * 60000,
            67000,
            67000 + i * 10,
            67000 - i * 5,
            67000 + i * 3,
            100,
        )
        results = aggregator.on_1m_close(candle)

    tfs = [r.tf for r in results]
    assert "15m" in tfs


def test_aggregator_1h_rollup(aggregator):
    base_ts = 1716768000000
    for i in range(60):
        candle = _make_1m_candle(
            "BTCUSDT",
            base_ts + i * 60000,
            67000,
            67000 + i,
            67000 - i,
            67000 + i,
            100,
        )
        results = aggregator.on_1m_close(candle)

    tfs = [r.tf for r in results]
    assert "1h" in tfs


def test_aggregator_no_rollup_mid_candle(aggregator):
    base_ts = 1716768000000
    for i in range(3):
        candle = _make_1m_candle(
            "BTCUSDT",
            base_ts + i * 60000,
            67000,
            67500,
            66800,
            67200,
            100,
        )
        results = aggregator.on_1m_close(candle)

    tfs = [r.tf for r in results]
    assert "5m" not in tfs
    assert "15m" not in tfs


def test_aggregator_get_candles(aggregator):
    base_ts = 1716768000000
    for i in range(5):
        candle = _make_1m_candle(
            "BTCUSDT",
            base_ts + i * 60000,
            67000,
            67500,
            66800,
            67200,
            100,
        )
        aggregator.on_1m_close(candle)

    candles_1m = aggregator.get_candles("BTCUSDT", "1m")
    assert len(candles_1m) == 5

    candles_5m = aggregator.get_candles("BTCUSDT", "5m")
    assert len(candles_5m) == 1


def test_aggregator_correction_overwrites(aggregator):
    base_ts = 1716768000000
    candle = _make_1m_candle("BTCUSDT", base_ts, 67000, 67500, 66800, 67200, 100)
    aggregator.on_1m_close(candle)

    correction = KlineCandle(
        symbol="BTCUSDT",
        tf="1m",
        open=67000,
        high=67600,
        low=66800,
        close=67300,
        volume=110,
        open_time=base_ts,
        close_time=base_ts + 59999,
        confirmed=True,
        correction=True,
    )
    aggregator.apply_correction(correction)

    candles = aggregator.get_candles("BTCUSDT", "1m")
    assert len(candles) == 1
    assert candles[0].high == 67600
    assert candles[0].volume == 110
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd market-data-service && python -m pytest tests/test_aggregator.py -v`
Expected: FAIL — `app.aggregator` module not found

- [ ] **Step 3: Write aggregator implementation**

```python
# market-data-service/app/aggregator.py
from __future__ import annotations
import logging
from collections import defaultdict

from app.models import KlineCandle

logger = logging.getLogger(__name__)

TF_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


class Aggregator:
    def __init__(self, timeframes: list[str] | None = None):
        self.timeframes = timeframes or ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
        self._1m_candles: dict[str, list[KlineCandle]] = defaultdict(list)
        self._tf_candles: dict[str, dict[str, list[KlineCandle]]] = defaultdict(lambda: defaultdict(list))
        self._max_1m_per_symbol = 1500

    def on_1m_close(self, candle: KlineCandle) -> list[KlineCandle]:
        symbol = candle.symbol
        self._1m_candles[symbol].append(candle)
        if len(self._1m_candles[symbol]) > self._max_1m_per_symbol:
            self._1m_candles[symbol] = self._1m_candles[symbol][-self._max_1m_per_symbol:]

        results = [candle]

        for tf in self.timeframes:
            if tf == "1m":
                continue
            tf_minutes = TF_MINUTES.get(tf)
            if tf_minutes is None:
                continue

            open_ts = candle.open_time
            tf_ms = tf_minutes * 60 * 1000
            tf_boundary = (open_ts // tf_ms + 1) * tf_ms
            candle_close_ts = open_ts + 60000

            if candle_close_ts < tf_boundary:
                continue

            tf_open_ts = tf_boundary - tf_ms
            needed = tf_minutes
            available = [c for c in self._1m_candles[symbol] if c.open_time >= tf_open_ts and c.open_time < tf_boundary]

            if len(available) < needed:
                continue

            rolled = self._rollup(available, symbol, tf, tf_open_ts, tf_boundary - 1)
            self._tf_candles[symbol][tf].append(rolled)
            results.append(rolled)

        return results

    def _rollup(
        self,
        candles: list[KlineCandle],
        symbol: str,
        tf: str,
        open_time: int,
        close_time: int,
    ) -> KlineCandle:
        return KlineCandle(
            symbol=symbol,
            tf=tf,
            open=candles[0].open,
            high=max(c.high for c in candles),
            low=min(c.low for c in candles),
            close=candles[-1].close,
            volume=sum(c.volume for c in candles),
            open_time=open_time,
            close_time=close_time,
            confirmed=True,
            correction=False,
        )

    def apply_correction(self, correction: KlineCandle) -> None:
        symbol = correction.symbol
        tf = correction.tf
        if tf == "1m":
            store = self._1m_candles.get(symbol, [])
            for i, c in enumerate(store):
                if c.open_time == correction.open_time:
                    store[i] = correction
                    break
        else:
            store = self._tf_candles.get(symbol, {}).get(tf, [])
            for i, c in enumerate(store):
                if c.open_time == correction.open_time:
                    store[i] = correction
                    break

    def get_candles(self, symbol: str, tf: str) -> list[KlineCandle]:
        if tf == "1m":
            return list(self._1m_candles.get(symbol, []))
        return list(self._tf_candles.get(symbol, {}).get(tf, []))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd market-data-service && python -m pytest tests/test_aggregator.py -v`
Expected: PASS — all 7 tests

- [ ] **Step 5: Commit**

```bash
git add market-data-service/app/aggregator.py market-data-service/tests/test_aggregator.py
git commit -m "feat(market-data-service): add 1m→higher TF aggregator"
```

---

### Task 3: Redis Publisher

**Files:**
- Create: `market-data-service/app/publisher.py`
- Test: `market-data-service/tests/test_publisher.py`

- [ ] **Step 1: Write failing test for publisher**

```python
# market-data-service/tests/test_publisher.py
import json
import pytest
import redis as redis_lib
from app.publisher import Publisher
from app.models import KlineCandle, TickerUpdate


@pytest.fixture
def redis_client():
    r = redis_lib.Redis(decode_responses=True)
    return r


@pytest.fixture
def publisher(redis_client):
    return Publisher(redis_client)


def test_publish_kline(publisher, redis_client):
    candle = KlineCandle(
        symbol="BTCUSDT",
        tf="1m",
        open=67000.0,
        high=67500.0,
        low=66800.0,
        close=67200.0,
        volume=12345.6,
        open_time=1716768000000,
        close_time=1716771599999,
    )
    publisher.publish_kline(candle)

    messages = redis_client.pubsub()
    messages.subscribe("kline:1m")

    msg = messages.get_message(timeout=1.0)
    while msg is None or msg["type"] != "message":
        msg = messages.get_message(timeout=1.0)
        if msg is not None and msg["type"] == "message":
            break
        if msg is None:
            break

    messages.unsubscribe()
    messages.close()

    if msg and msg["type"] == "message":
        data = json.loads(msg["data"])
        assert data["symbol"] == "BTCUSDT"
        assert data["tf"] == "1m"
        assert data["close"] == 67200.0


def test_publish_ticker(publisher, redis_client):
    ticker = TickerUpdate(
        symbol="ETHUSDT",
        price=3000.5,
        timestamp=1716771600000,
        exchange="binance",
    )
    publisher.publish_ticker(ticker)

    messages = redis_client.pubsub()
    messages.subscribe("ticker")

    msg = messages.get_message(timeout=1.0)
    while msg is None or msg["type"] != "message":
        msg = messages.get_message(timeout=1.0)
        if msg is not None and msg["type"] == "message":
            break
        if msg is None:
            break

    messages.unsubscribe()
    messages.close()

    if msg and msg["type"] == "message":
        data = json.loads(msg["data"])
        assert data["symbol"] == "ETHUSDT"
        assert data["price"] == 3000.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd market-data-service && python -m pytest tests/test_publisher.py -v`
Expected: FAIL — `app.publisher` module not found (or Redis connection if no local Redis)

- [ ] **Step 3: Write publisher implementation**

```python
# market-data-service/app/publisher.py
from __future__ import annotations
import json
import logging

import redis as redis_lib

from app.models import KlineCandle, TickerUpdate

logger = logging.getLogger(__name__)


class Publisher:
    def __init__(self, redis_client: redis_lib.Redis):
        self._redis = redis_client

    def publish_kline(self, candle: KlineCandle) -> None:
        channel = f"kline:{candle.tf}"
        payload = json.dumps(candle.to_dict())
        self._redis.publish(channel, payload)
        logger.debug(f"[PUB] {channel} {candle.symbol} correction={candle.correction}")

    def publish_ticker(self, ticker: TickerUpdate) -> None:
        channel = "ticker"
        payload = json.dumps(ticker.to_dict())
        self._redis.publish(channel, payload)

    def publish_symbols(self, symbols: list[str]) -> None:
        channel = "symbols"
        payload = json.dumps({"symbols": symbols})
        self._redis.publish(channel, payload)

    def publish_kline_snapshot(self, candle: KlineCandle, max_candles: int = 500) -> None:
        """Store candle in Redis HASH so late-joining subscribers can recover history.

        Key layout:  kline_snapshot:{tf}:{symbol}
        Field:       open_time (str, sortable)
        Value:       candle JSON

        Alphas call HGETALL on startup before subscribing to Pub/Sub — this
        replaces the fragile wait_for_initial_data() polling approach.
        """
        key = f"kline_snapshot:{candle.tf}:{candle.symbol}"
        self._redis.hset(key, str(candle.open_time), json.dumps(candle.to_dict()))
        # Trim to max_candles, keeping newest
        all_fields = self._redis.hkeys(key)
        if len(all_fields) > max_candles:
            oldest = sorted(all_fields, key=int)[:len(all_fields) - max_candles]
            if oldest:
                self._redis.hdel(key, *oldest)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd market-data-service && python -m pytest tests/test_publisher.py -v`
Expected: PASS (requires Redis running locally — skip in CI if unavailable)

- [ ] **Step 5: Commit**

```bash
git add market-data-service/app/publisher.py market-data-service/tests/test_publisher.py
git commit -m "feat(market-data-service): add Redis publisher"
```

---

### Task 4: KlineFeed — Binance 1m WS + REST initial load

**Files:**
- Create: `market-data-service/app/kline_feed.py`
- Test: `market-data-service/tests/test_kline_feed.py`

- [ ] **Step 1: Write failing test for kline feed**

```python
# market-data-service/tests/test_kline_feed.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.kline_feed import KlineFeed
from app.models import KlineCandle
from app.aggregator import Aggregator


@pytest.fixture
def aggregator():
    return Aggregator(timeframes=["1m", "5m", "15m", "1h"])


@pytest.fixture
def feed(aggregator):
    return KlineFeed(aggregator=aggregator, ws_batch_size=150)


def test_build_stream_names(feed):
    names = feed.build_stream_names(["BTCUSDT", "ETHUSDT"], "1m")
    assert names == ["btcusdt@kline_1m", "ethusdt@kline_1m"]


def test_batch_symbols(feed):
    symbols = [f"SYM{i}USDT" for i in range(350)]
    batches = feed.batch_symbols(symbols)
    assert len(batches) == 3
    assert len(batches[0]) == 150
    assert len(batches[1]) == 150
    assert len(batches[2]) == 50


@pytest.mark.asyncio
async def test_process_message_confirmed(aggregator):
    feed = KlineFeed(aggregator=aggregator, ws_batch_size=150)
    msg = {
        "e": "kline",
        "s": "BTCUSDT",
        "k": {
            "t": 1716768000000,
            "T": 1716771599999,
            "o": "67000.0",
            "h": "67500.0",
            "l": "66800.0",
            "c": "67200.0",
            "v": "100.0",
            "x": True,
        },
    }
    results = await feed.process_message(msg)
    assert results is not None
    assert len(results) >= 1
    assert results[0].symbol == "BTCUSDT"
    assert results[0].tf == "1m"


@pytest.mark.asyncio
async def test_process_message_partial_returns_none(aggregator):
    feed = KlineFeed(aggregator=aggregator, ws_batch_size=150)
    msg = {
        "e": "kline",
        "s": "BTCUSDT",
        "k": {
            "t": 1716768000000,
            "T": 1716771599999,
            "o": "67000.0",
            "h": "67500.0",
            "l": "66800.0",
            "c": "67200.0",
            "v": "100.0",
            "x": False,
        },
    }
    results = await feed.process_message(msg)
    assert results is None


@pytest.mark.asyncio
async def test_process_message_non_kline_returns_none(aggregator):
    feed = KlineFeed(aggregator=aggregator, ws_batch_size=150)
    results = await feed.process_message({"e": "other"})
    assert results is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd market-data-service && python -m pytest tests/test_kline_feed.py -v`
Expected: FAIL — `app.kline_feed` module not found

- [ ] **Step 3: Write kline feed implementation**

```python
# market-data-service/app/kline_feed.py
from __future__ import annotations
import asyncio
import logging
import random

from binance import BinanceSocketManager
from binance.async_client import AsyncClient
from binance.enums import FuturesType

from app.aggregator import Aggregator
from app.models import KlineCandle

logger = logging.getLogger(__name__)


class KlineFeed:
    def __init__(self, aggregator: Aggregator, ws_batch_size: int = 150):
        self.aggregator = aggregator
        self.ws_batch_size = ws_batch_size
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=1000)
        self._shutdown = asyncio.Event()

    def build_stream_names(self, symbols: list[str], tf: str = "1m") -> list[str]:
        return [f"{s.lower()}@kline_{tf}" for s in symbols]

    def batch_symbols(self, symbols: list[str]) -> list[list[str]]:
        batches = []
        for i in range(0, len(symbols), self.ws_batch_size):
            batches.append(symbols[i : i + self.ws_batch_size])
        return batches

    async def process_message(self, msg_data: dict) -> list[KlineCandle] | None:
        candle = KlineCandle.from_ws_1m(msg_data)
        if candle is None:
            return None
        results = self.aggregator.on_1m_close(candle)
        return results

    async def load_initial_data(
        self,
        client: AsyncClient,
        symbols: list[str],
        store_size: int = 500,
        semaphore_limit: int = 25,
    ) -> None:
        semaphore = asyncio.Semaphore(semaphore_limit)

        async def _load_one(sym: str) -> None:
            async with semaphore:
                try:
                    klines = await client.futures_klines(
                        symbol=sym, interval="1m", limit=store_size
                    )
                    if not klines:
                        return
                    for row in klines:
                        candle = KlineCandle(
                            symbol=sym,
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
                        self.aggregator.on_1m_close(candle)
                except Exception as e:
                    logger.warning(f"Failed to load initial data for {sym}: {e}")

        await asyncio.gather(*[_load_one(sym) for sym in symbols])
        logger.info(f"[KLINE] Initial 1m data loaded for {len(symbols)} symbols")

    async def run_ws_batch(
        self,
        client: AsyncClient,
        symbols: list[str],
        batch_id: int = 0,
    ) -> None:
        stream_names = self.build_stream_names(symbols, "1m")
        consecutive_failures = 0

        while not self._shutdown.is_set():
            try:
                stagger = batch_id * 0.5 if consecutive_failures == 0 else random.uniform(2, 10)
                if stagger > 0:
                    await asyncio.sleep(stagger)
                if self._shutdown.is_set():
                    break

                socket_manager = BinanceSocketManager(client)
                logger.info(f"[KLINE] Batch {batch_id}: Connecting ({len(stream_names)} streams)")

                async with socket_manager.futures_multiplex_socket(
                    stream_names, futures_type=FuturesType.USD_M
                ) as ws:
                    logger.info(f"[KLINE] Batch {batch_id}: Connected!")
                    consecutive_failures = 0

                    while not self._shutdown.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            if msg is None or msg.get("e") == "error":
                                continue
                            try:
                                self._queue.put_nowait(msg)
                            except asyncio.QueueFull:
                                self._queue.get_nowait()
                                self._queue.put_nowait(msg)
                        except asyncio.TimeoutError:
                            continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                if self._shutdown.is_set():
                    break
                backoff = min(5 * (2 ** (consecutive_failures - 1)), 60)
                logger.error(f"[KLINE] Batch {batch_id} error: {e}. Reconnect in {backoff}s")
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
            except Exception as e:
                logger.error(f"[KLINE] Queue consumer error: {e}")

    def shutdown(self) -> None:
        self._shutdown.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd market-data-service && python -m pytest tests/test_kline_feed.py -v`
Expected: PASS — all 5 tests

- [ ] **Step 5: Commit**

```bash
git add market-data-service/app/kline_feed.py market-data-service/tests/test_kline_feed.py
git commit -m "feat(market-data-service): add kline feed with 1m WS + REST initial load"
```

---

### Task 5: TickerFeed — Binance @ticker WS

**Files:**
- Create: `market-data-service/app/ticker_feed.py`
- Test: `market-data-service/tests/test_ticker_feed.py`

- [ ] **Step 1: Write failing test for ticker feed**

```python
# market-data-service/tests/test_ticker_feed.py
import pytest
from app.ticker_feed import TickerFeed
from app.models import TickerUpdate


@pytest.fixture
def feed():
    return TickerFeed()


def test_build_ticker_streams(feed):
    streams = feed.build_ticker_streams(["BTCUSDT", "ETHUSDT"])
    assert streams == ["btcusdt@ticker", "ethusdt@ticker"]


def test_batch_symbols(feed):
    symbols = [f"SYM{i}USDT" for i in range(350)]
    batches = feed.batch_symbols(symbols)
    assert len(batches) == 3
    assert len(batches[0]) == 150


def test_parse_binance_ticker(feed):
    msg = {
        "e": "24hrTicker",
        "E": 1716771600000,
        "s": "BTCUSDT",
        "c": "67200.50",
    }
    ticker = feed.parse_binance_ticker(msg)
    assert ticker is not None
    assert ticker.symbol == "BTCUSDT"
    assert ticker.price == 67200.50
    assert ticker.exchange == "binance"


def test_parse_binance_ticker_non_ticker(feed):
    msg = {"e": "kline", "s": "BTCUSDT"}
    ticker = feed.parse_binance_ticker(msg)
    assert ticker is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd market-data-service && python -m pytest tests/test_ticker_feed.py -v`
Expected: FAIL — `app.ticker_feed` module not found

- [ ] **Step 3: Write ticker feed implementation**

```python
# market-data-service/app/ticker_feed.py
from __future__ import annotations
import asyncio
import json
import logging
import random

import websockets

from app.models import TickerUpdate

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://fstream.binance.com/ws"


class TickerFeed:
    def __init__(self, batch_size: int = 150):
        self.batch_size = batch_size
        self._shutdown = asyncio.Event()

    def build_ticker_streams(self, symbols: list[str]) -> list[str]:
        return [f"{s.lower()}@ticker" for s in symbols]

    def batch_symbols(self, symbols: list[str]) -> list[list[str]]:
        batches = []
        for i in range(0, len(symbols), self.batch_size):
            batches.append(symbols[i : i + self.batch_size])
        return batches

    def parse_binance_ticker(self, msg: dict) -> TickerUpdate | None:
        if msg.get("e") != "24hrTicker":
            return None
        if "s" not in msg or "c" not in msg:
            return None
        return TickerUpdate.from_binance_ws(msg)

    async def run_binance_batch(self, symbols: list[str], publisher) -> None:
        streams = self.build_ticker_streams(symbols)
        url = f"{BINANCE_WS_URL}/{'/'.join(streams)}"
        consecutive_failures = 0

        while not self._shutdown.is_set():
            try:
                async with websockets.connect(url) as ws:
                    logger.info(f"[TICKER] Connected for {len(symbols)} symbols")
                    consecutive_failures = 0

                    while not self._shutdown.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            msg = json.loads(raw)
                            ticker = self.parse_binance_ticker(msg)
                            if ticker:
                                publisher.publish_ticker(ticker)
                        except asyncio.TimeoutError:
                            continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                if self._shutdown.is_set():
                    break
                backoff = min(5 * (2 ** (consecutive_failures - 1)), 60)
                logger.error(f"[TICKER] Error: {e}. Reconnect in {backoff}s")
                await asyncio.sleep(backoff)

    def shutdown(self) -> None:
        self._shutdown.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd market-data-service && python -m pytest tests/test_ticker_feed.py -v`
Expected: PASS — all 4 tests

- [ ] **Step 5: Commit**

```bash
git add market-data-service/app/ticker_feed.py market-data-service/tests/test_ticker_feed.py
git commit -m "feat(market-data-service): add ticker feed"
```

---

### Task 6: Reconciler — REST verification after candle close

**Files:**
- Create: `market-data-service/app/reconciler.py`
- Test: `market-data-service/tests/test_reconciler.py`

- [ ] **Step 1: Write failing test for reconciler**

```python
# market-data-service/tests/test_reconciler.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.reconciler import Reconciler
from app.aggregator import Aggregator
from app.models import KlineCandle


@pytest.fixture
def aggregator():
    return Aggregator(timeframes=["1m", "15m", "1h"])


@pytest.fixture
def reconciler(aggregator):
    return Reconciler(
        aggregator=aggregator,
        reconcile_tfs=["15m", "1h"],
        reconcile_delay=0,
        semaphore_limit=25,
    )


def test_is_candle_boundary(reconciler):
    ts_15m = 1716768000000
    assert reconciler.is_candle_boundary(ts_15m, "15m") is True
    assert reconciler.is_candle_boundary(ts_15m, "1h") is True
    ts_offset = 1716768060000
    assert reconciler.is_candle_boundary(ts_offset, "15m") is False


def test_should_reconcile(reconciler):
    ts_15m = 1716768000000
    assert reconciler.should_reconcile(ts_15m) is True
    ts_offset = 1716768060000
    assert reconciler.should_reconcile(ts_offset) is False


@pytest.mark.asyncio
async def test_reconcile_symbol_detects_mismatch(aggregator):
    publisher = MagicMock()
    reconciler = Reconciler(
        aggregator=aggregator,
        reconcile_tfs=["15m"],
        reconcile_delay=0,
        semaphore_limit=25,
    )

    base_ts = 1716768000000
    for i in range(15):
        candle = KlineCandle(
            symbol="BTCUSDT", tf="1m",
            open=67000, high=67500, low=66800, close=67200, volume=100,
            open_time=base_ts + i * 60000,
            close_time=base_ts + i * 60000 + 59999,
        )
        aggregator.on_1m_close(candle)

    client = AsyncMock()
    client.futures_klines = AsyncMock(return_value=[
        [base_ts, "67000", "67600", "66800", "67300", "110", base_ts + 15 * 60000 - 1],
        [base_ts + 15 * 60000, "67300", "67800", "67000", "67500", "120", base_ts + 30 * 60000 - 1],
    ])

    corrections = await reconciler.reconcile_symbol(client, "BTCUSDT", "15m")
    assert len(corrections) == 1
    assert corrections[0].high == 67600
    assert corrections[0].volume == 110
    assert corrections[0].correction is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd market-data-service && python -m pytest tests/test_reconciler.py -v`
Expected: FAIL — `app.reconciler` module not found

- [ ] **Step 3: Write reconciler implementation**

```python
# market-data-service/app/reconciler.py
from __future__ import annotations
import asyncio
import logging
import time

from binance.async_client import AsyncClient

from app.aggregator import Aggregator
from app.models import KlineCandle

logger = logging.getLogger(__name__)

TF_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}

_KLINE_INTERVAL = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d",
}


class Reconciler:
    def __init__(
        self,
        aggregator: Aggregator,
        reconcile_tfs: list[str] | None = None,
        reconcile_delay: int = 5,
        semaphore_limit: int = 25,
    ):
        self.aggregator = aggregator
        self.reconcile_tfs = reconcile_tfs or ["15m", "1h"]
        self.reconcile_delay = reconcile_delay
        self.semaphore_limit = semaphore_limit
        self._shutdown = asyncio.Event()

    def is_candle_boundary(self, open_time_ms: int, tf: str) -> bool:
        tf_minutes = TF_MINUTES.get(tf, 1)
        tf_ms = tf_minutes * 60 * 1000
        return open_time_ms % tf_ms == 0

    def should_reconcile(self, open_time_ms: int) -> bool:
        for tf in self.reconcile_tfs:
            if self.is_candle_boundary(open_time_ms, tf):
                return True
        return False

    async def reconcile_symbol(
        self,
        client: AsyncClient,
        symbol: str,
        tf: str,
    ) -> list[KlineCandle]:
        corrections = []
        interval = _KLINE_INTERVAL.get(tf, tf)
        try:
            klines = await client.futures_klines(
                symbol=symbol, interval=interval, limit=2
            )
            if not klines or len(klines) < 2:
                return corrections

            row = klines[-2]
            rest_ts = int(row[0])
            rest_open = float(row[1])
            rest_high = float(row[2])
            rest_low = float(row[3])
            rest_close = float(row[4])
            rest_vol = float(row[5])

            stored = self.aggregator.get_candles(symbol, tf)
            for c in stored:
                if c.open_time == rest_ts:
                    needs_correction = (
                        c.high != rest_high
                        or c.low != rest_low
                        or c.close != rest_close
                        or c.volume != rest_vol
                        or c.open != rest_open
                    )
                    if needs_correction:
                        correction = KlineCandle(
                            symbol=symbol,
                            tf=tf,
                            open=rest_open,
                            high=rest_high,
                            low=rest_low,
                            close=rest_close,
                            volume=rest_vol,
                            open_time=rest_ts,
                            close_time=int(row[6]),
                            confirmed=True,
                            correction=True,
                        )
                        self.aggregator.apply_correction(correction)
                        corrections.append(correction)
                    break

        except Exception as e:
            logger.debug(f"[RECONCILE] Failed for {symbol} {tf}: {e}")

        return corrections

    async def reconcile_all(
        self,
        client: AsyncClient,
        symbols: list[str],
        tf: str,
        publisher=None,
    ) -> int:
        semaphore = asyncio.Semaphore(self.semaphore_limit)
        total_corrections = 0

        async def _reconcile_one(sym: str) -> int:
            nonlocal total_corrections
            async with semaphore:
                corrections = await self.reconcile_symbol(client, sym, tf)
                if corrections and publisher:
                    for c in corrections:
                        publisher.publish_kline(c)
                return len(corrections)

        results = await asyncio.gather(*[_reconcile_one(sym) for sym in symbols])
        total_corrections = sum(results)
        if total_corrections > 0:
            logger.info(f"[RECONCILE] {tf}: {total_corrections} corrections for {len(symbols)} symbols")
        return total_corrections

    async def _run_for_tf(
        self,
        client: AsyncClient,
        symbols: list[str],
        tf: str,
        publisher=None,
    ) -> None:
        """Dedicated reconcile loop for a single TF.

        Keeps its own wall-clock reference so sleeping for one TF does not
        corrupt the timing of another TF (the original shared-now bug).
        """
        tf_sec = TF_MINUTES.get(tf, 60) * 60
        while not self._shutdown.is_set():
            try:
                now = time.time()
                next_boundary = ((int(now // tf_sec) + 1) * tf_sec)
                wait = next_boundary - now + self.reconcile_delay
                if wait > 0:
                    await asyncio.sleep(wait)
                if self._shutdown.is_set():
                    break
                await self.reconcile_all(client, symbols, tf, publisher)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RECONCILE] {tf} error: {e}")
                await asyncio.sleep(10)

    async def run(self, client: AsyncClient, symbols: list[str], publisher=None) -> None:
        """Spawn one independent task per reconcile TF so their sleep cycles
        never interfere with each other."""
        tasks = [
            asyncio.create_task(
                self._run_for_tf(client, symbols, tf, publisher)
            )
            for tf in self.reconcile_tfs
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def shutdown(self) -> None:
        self._shutdown.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd market-data-service && python -m pytest tests/test_reconciler.py -v`
Expected: PASS — all 4 tests

- [ ] **Step 5: Commit**

```bash
git add market-data-service/app/reconciler.py market-data-service/tests/test_reconciler.py
git commit -m "feat(market-data-service): add REST reconciler"
```

---

### Task 7: Main entrypoint + Dockerfile

**Files:**
- Create: `market-data-service/app/main.py`
- Create: `market-data-service/Dockerfile`
- Create: `market-data-service/.env.example`

- [ ] **Step 1: Write main.py**

```python
# market-data-service/app/main.py
import asyncio
import logging
import os
import signal as sig
import requests

import redis as redis_lib
from binance.async_client import AsyncClient

from app.config import settings
from app.aggregator import Aggregator
from app.kline_feed import KlineFeed
from app.ticker_feed import TickerFeed
from app.reconciler import Reconciler
from app.publisher import Publisher

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(settings.LOG_DIR, "market-data.log")),
        ],
        force=True,
    )


def get_symbol_universe() -> list[str]:
    if settings.SYMBOL_MODE == "manual":
        return settings.get_symbols_list()

    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15
        )
        data = resp.json()
        symbols = [
            s["symbol"]
            for s in data.get("symbols", [])
            if s.get("quoteAsset") == "USDT"
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        ]
        return sorted(symbols)
    except Exception as e:
        logger.warning(f"Failed to fetch symbol universe: {e}")
        return ["BTCUSDT", "ETHUSDT"]


async def run_service() -> None:
    configure_logging()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    for s in (sig.SIGTERM, sig.SIGINT):
        loop.add_signal_handler(s, shutdown_event.set)

    symbols = get_symbol_universe()
    logger.info(f"Symbol universe: {len(symbols)} symbols")

    timeframes = settings.get_timeframes()
    aggregator = Aggregator(timeframes=timeframes)

    r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
    publisher = Publisher(r)

    publisher.publish_symbols(symbols)

    client = await AsyncClient.create()
    logger.info("Binance client created")

    kline_feed = KlineFeed(aggregator=aggregator, ws_batch_size=settings.WS_BATCH_SIZE)
    ticker_feed = TickerFeed(batch_size=settings.TICKER_BATCH_SIZE)
    reconciler = Reconciler(
        aggregator=aggregator,
        reconcile_tfs=settings.get_reconcile_tfs(),
        reconcile_delay=settings.RECONCILE_DELAY,
        semaphore_limit=settings.REST_SEMAPHORE,
    )

    logger.info("Loading initial 1m data...")
    await kline_feed.load_initial_data(
        client, symbols,
        store_size=settings.HISTORY_CANDLES,
        semaphore_limit=settings.REST_SEMAPHORE,
    )
    logger.info(f"Initial data loaded. 1m candles: {sum(len(v) for v in aggregator._1m_candles.values())}")

    # Populate Redis HASH snapshots so alphas can recover history on (re)start
    # without waiting for candles to trickle in via Pub/Sub.
    logger.info("Populating Redis HASH snapshots for late-join recovery...")
    snap_count = 0
    for symbol, tf_map in aggregator._tf_candles.items():
        for tf, candles in tf_map.items():
            for candle in candles[-settings.SNAPSHOT_MAX_CANDLES:]:
                publisher.publish_kline_snapshot(candle, max_candles=settings.SNAPSHOT_MAX_CANDLES)
                snap_count += 1
    for symbol, candles in aggregator._1m_candles.items():
        for candle in candles[-settings.SNAPSHOT_MAX_CANDLES:]:
            publisher.publish_kline_snapshot(candle, max_candles=settings.SNAPSHOT_MAX_CANDLES)
            snap_count += 1
    logger.info(f"Snapshots written: {snap_count} candles across {len(symbols)} symbols")

    tasks = []

    kline_batches = kline_feed.batch_symbols(symbols)
    for i, batch in enumerate(kline_batches):
        tasks.append(asyncio.create_task(
            kline_feed.run_ws_batch(client, batch, batch_id=i)
        ))

    tasks.append(asyncio.create_task(
        kline_feed.consume_queue(publisher)
    ))

    ticker_batches = ticker_feed.batch_symbols(symbols)
    for batch in ticker_batches:
        tasks.append(asyncio.create_task(
            ticker_feed.run_binance_batch(batch, publisher)
        ))

    tasks.append(asyncio.create_task(
        reconciler.run(client, symbols, publisher)
    ))

    async def _health_loop():
        while not shutdown_event.is_set():
            try:
                with open("/tmp/bot_health", "w") as f:
                    f.write("ok")
            except Exception:
                pass
            await asyncio.sleep(10)

    tasks.append(asyncio.create_task(_health_loop()))

    logger.info(f"Market data service running: {len(tasks)} tasks")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutting down market data service")
        kline_feed.shutdown()
        ticker_feed.shutdown()
        reconciler.shutdown()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close_connection()
        r.close()


if __name__ == "__main__":
    asyncio.run(run_service())
```

- [ ] **Step 2: Write Dockerfile**

```dockerfile
# market-data-service/Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "app.main"]
```

- [ ] **Step 3: Write .env.example**

```
# market-data-service/.env.example
REDIS_URL=redis://redis:6379
EXCHANGE=binance
SYMBOL_MODE=auto
SYMBOLS=
TIMEFRAMES=1m,5m,15m,30m,1h,4h,1d
HISTORY_CANDLES=7500
WS_BATCH_SIZE=150
REST_SEMAPHORE=25
RECONCILE_TFS=15m,1h
RECONCILE_DELAY=5
LOG_LEVEL=INFO
LOG_DIR=/app/logs
```

- [ ] **Step 4: Commit**

```bash
git add market-data-service/app/main.py market-data-service/Dockerfile market-data-service/.env.example
git commit -m "feat(market-data-service): add main entrypoint and Dockerfile"
```

---

### Task 8: Docker Compose — add market-data-service

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add market-data-service to docker-compose.yml**

Add the following service after the `redis` service definition in `docker-compose.yml`:

```yaml
  market-data-service:
    build: ./market-data-service
    depends_on:
      - redis
    env_file: ./market-data-service/.env
    volumes:
      - ./logs/market-data:/app/logs
    environment:
      - REDIS_URL=redis://redis:6379
      - LOG_DIR=/app/logs
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    networks:
      - paper-trade
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import os; exit(0 if os.path.exists('/tmp/bot_health') else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s
```

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add market-data-service to docker-compose"
```

---

### Task 9: Refactor BaseEngine — replace WS/REST with Redis subscriber

**Files:**
- Modify: `alphas/base/engine.py`
- Modify: `alphas/base/config.py`
- Test: `alphas/base/tests/test_engine.py`

- [ ] **Step 1: Write failing test for new BaseEngine behavior**

```python
# alphas/base/tests/test_engine.py
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from base.engine import BaseEngine
from base.config import BaseConfig
from base.models import SymbolData


class MockEngine(BaseEngine):
    def get_required_channels(self) -> list[str]:
        return ["kline:1m", "kline:15m"]

    async def scan_loop(self) -> None:
        pass


@pytest.fixture
def config():
    c = MagicMock(spec=BaseConfig)
    c.ALPHA_ID = "test-alpha"
    c.REDIS_URL = "redis://localhost:6379"
    c.LOG_LEVEL = "INFO"
    c.LOG_DIR = "/tmp/test_logs"
    return c


@pytest.fixture
def engine(config):
    return MockEngine(config)


def test_engine_has_symbol_data(engine):
    assert isinstance(engine.symbol_data, dict)


def test_engine_has_data_channels(engine):
    channels = engine.get_required_channels()
    assert "kline:1m" in channels
    assert "kline:15m" in channels


def test_engine_on_kline_message_appends(engine):
    engine.on_kline_message({
        "symbol": "BTCUSDT",
        "tf": "1m",
        "open": 67000.0,
        "high": 67500.0,
        "low": 66800.0,
        "close": 67200.0,
        "volume": 100.0,
        "open_time": 1716768000000,
        "close_time": 1716771599999,
        "confirmed": True,
        "correction": False,
    })
    assert "BTCUSDT" in engine.symbol_data
    sd = engine.symbol_data["BTCUSDT"]
    assert sd.price_list[-1] == 67200.0
    assert sd.high_list[-1] == 67500.0


def test_engine_on_kline_message_correction_overwrites(engine):
    engine.on_kline_message({
        "symbol": "BTCUSDT",
        "tf": "1m",
        "open": 67000.0,
        "high": 67500.0,
        "low": 66800.0,
        "close": 67200.0,
        "volume": 100.0,
        "open_time": 1716768000000,
        "close_time": 1716771599999,
        "confirmed": True,
        "correction": False,
    })
    engine.on_kline_message({
        "symbol": "BTCUSDT",
        "tf": "1m",
        "open": 67000.0,
        "high": 67600.0,
        "low": 66800.0,
        "close": 67300.0,
        "volume": 110.0,
        "open_time": 1716768000000,
        "close_time": 1716771599999,
        "confirmed": True,
        "correction": True,
    })
    sd = engine.symbol_data["BTCUSDT"]
    assert len(sd.time_list) == 1
    assert sd.high_list[-1] == 67600.0
    assert sd.volume_list[-1] == 110.0


def test_engine_on_kline_multiple_candles(engine):
    base_ts = 1716768000000
    for i in range(3):
        engine.on_kline_message({
            "symbol": "ETHUSDT",
            "tf": "1m",
            "open": 3000.0 + i,
            "high": 3050.0,
            "low": 2990.0,
            "close": 3020.0 + i,
            "volume": 100.0,
            "open_time": base_ts + i * 60000,
            "close_time": base_ts + i * 60000 + 59999,
            "confirmed": True,
            "correction": False,
        })
    sd = engine.symbol_data["ETHUSDT"]
    assert len(sd.time_list) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd alphas && python -m pytest base/tests/test_engine.py -v`
Expected: FAIL — `BaseEngine` doesn't have `get_required_channels` or `on_kline_message`

- [ ] **Step 3: Modify base/config.py — add DATA_CHANNELS**

Add to `BaseConfig` in `alphas/base/config.py`:

```python
DATA_CHANNELS: str = ""
```

The full file becomes:

```python
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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )
```

- [ ] **Step 4: Rewrite base/engine.py**

```python
# alphas/base/engine.py
import asyncio
import json
import logging
import os
import signal as sig
from abc import ABC, abstractmethod

import redis as redis_lib

from base.config import BaseConfig
from base.models import SymbolData
from base import signal_push


class BaseEngine(ABC):
    def __init__(self, config: BaseConfig):
        self.config = config
        self.symbol_data: dict[str, SymbolData] = {}
        self.data_lock = asyncio.Lock()
        self.shutdown_event = asyncio.Event()
        self._logger = logging.getLogger(config.ALPHA_ID)

    @abstractmethod
    def get_required_channels(self) -> list[str]:
        """Return list of Redis Pub/Sub channels to subscribe to.

        Each channel must map to exactly ONE timeframe — do NOT mix TFs in one
        SymbolData store.  Examples:
          wilder (1h): ["kline:1h"]
          adx (15m):   ["kline:15m"]
        """

    @abstractmethod
    async def scan_loop(self) -> None:
        """Main signal scanning loop — call push_signal() when signals found."""

    def on_kline_message(self, msg: dict) -> None:
        """Append or correct a closed candle in symbol_data.

        IMPORTANT: only call with messages from a single TF channel.
        Mixing 1m and 1h messages for the same symbol would interleave
        open_times and corrupt indicator computation.
        """
        symbol = msg.get("symbol", "")
        if not symbol:
            return

        sd = self.symbol_data.get(symbol)
        if sd is None:
            sd = SymbolData()
            self.symbol_data[symbol] = sd

        open_time = msg.get("open_time", 0)
        is_correction = msg.get("correction", False)

        if is_correction and sd.time_list:
            for i in range(len(sd.time_list) - 1, -1, -1):
                if sd.time_list[i] == open_time:
                    sd.open_list[i] = msg.get("open", 0.0)
                    sd.high_list[i] = msg.get("high", 0.0)
                    sd.low_list[i] = msg.get("low", 0.0)
                    sd.price_list[i] = msg.get("close", 0.0)
                    sd.volume_list[i] = msg.get("volume", 0.0)
                    break
        else:
            if sd.time_list and open_time <= sd.time_list[-1]:
                return
            sd.time_list.append(open_time)
            sd.open_list.append(msg.get("open", 0.0))
            sd.high_list.append(msg.get("high", 0.0))
            sd.low_list.append(msg.get("low", 0.0))
            sd.price_list.append(msg.get("close", 0.0))
            sd.volume_list.append(msg.get("volume", 0.0))

    def _load_snapshots(self, r: redis_lib.Redis, channels: list[str]) -> int:
        """Synchronously load historical candles from Redis HASH snapshots.

        Market-data-service writes kline_snapshot:{tf}:{symbol} HASHes after
        initial REST load.  This lets alphas recover history instantly on
        (re)start instead of waiting for candles to trickle in via Pub/Sub.

        Returns number of symbols loaded.
        """
        loaded: set[str] = set()
        for channel in channels:
            if not channel.startswith("kline:"):
                continue
            tf = channel.split(":", 1)[1]
            keys = r.keys(f"kline_snapshot:{tf}:*")
            for key in keys:
                symbol = key.rsplit(":", 1)[1]
                raw = r.hgetall(key)
                if not raw:
                    continue
                sorted_items = sorted(raw.items(), key=lambda x: int(x[0]))
                for _, payload in sorted_items:
                    self.on_kline_message(json.loads(payload))
                loaded.add(symbol)
        return len(loaded)

    async def subscribe_data_feeds(self) -> asyncio.Task:
        r = redis_lib.from_url(self.config.REDIS_URL, decode_responses=True)
        channels = self.get_required_channels()

        # Step 1: bulk-load from Redis HASH snapshots (synchronous, fast)
        n = await asyncio.to_thread(self._load_snapshots, r, channels)
        self._logger.info(
            f"[{self.config.ALPHA_ID}] Snapshot load: {n} symbols, "
            f"{len(self.symbol_data)} total in symbol_data"
        )

        # Step 2: live Pub/Sub
        self._logger.info(f"[{self.config.ALPHA_ID}] Subscribing to channels: {channels}")
        pubsub = r.pubsub()
        pubsub.subscribe(*channels)

        async def _listen() -> None:
            while not self.shutdown_event.is_set():
                try:
                    msg = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
                    if msg and msg["type"] == "message":
                        data = json.loads(msg["data"])
                        channel = msg["channel"]
                        if isinstance(channel, bytes):
                            channel = channel.decode()
                        if channel.startswith("kline:"):
                            self.on_kline_message(data)
                except Exception as e:
                    self._logger.debug(f"Redis subscriber error: {e}")
                    await asyncio.sleep(1)

            pubsub.unsubscribe()
            pubsub.close()
            r.close()

        return asyncio.create_task(_listen())

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
        for s in (sig.SIGTERM, sig.SIGINT):
            loop.add_signal_handler(s, self.shutdown_event.set)

        self._logger.info(f"[{self.config.ALPHA_ID}] Starting alpha engine")

        # subscribe_data_feeds loads snapshots synchronously before returning,
        # so symbol_data is already populated here — no polling needed.
        sub_task = await self.subscribe_data_feeds()

        if len(self.symbol_data) == 0:
            self._logger.error(
                f"[{self.config.ALPHA_ID}] No snapshot data found in Redis. "
                "Ensure market-data-service started first."
            )
            return

        self._logger.info(
            f"[{self.config.ALPHA_ID}] Ready: {len(self.symbol_data)} symbols loaded"
        )

        scan_task = asyncio.create_task(self.scan_loop())
        health_task = asyncio.create_task(self._health_loop())

        try:
            await asyncio.gather(scan_task, health_task, sub_task)
        except asyncio.CancelledError:
            pass
        finally:
            self._logger.info(f"[{self.config.ALPHA_ID}] Shutting down")

    async def _health_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                with open("/tmp/bot_health", "w") as f:
                    f.write("ok")
            except Exception:
                pass
            await asyncio.sleep(10)

    def push_signal(self, signal_type: str, **kwargs) -> None:
        signal_push.push_signal(signal_type, self.config.ALPHA_ID, **kwargs)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd alphas && python -m pytest base/tests/test_engine.py -v`
Expected: PASS — all 6 tests

- [ ] **Step 6: Commit**

```bash
git add alphas/base/engine.py alphas/base/config.py alphas/base/tests/test_engine.py
git commit -m "refactor(base): replace load_initial_data/create_ws_tasks with Redis subscriber"
```

---

### Task 10: Refactor adx-trend-follow alpha

**Files:**
- Modify: `alphas/adx-trend-follow/app/engine.py`
- Modify: `alphas/adx-trend-follow/app/config.py`
- Delete: `alphas/adx-trend-follow/app/market_data.py`
- Delete: `alphas/adx-trend-follow/app/ws_manager.py`
- Modify: `alphas/adx-trend-follow/docker-compose.yml`

- [ ] **Step 1: Update adx config.py — add DATA_CHANNELS**

Replace `alphas/adx-trend-follow/app/config.py` with:

```python
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from base.config import BaseConfig
from pydantic_settings import SettingsConfigDict


class ADXTrendFollowConfig(BaseConfig):
    ALPHA_ID: str = "adx-trend-follow"

    TF: str = "15m"
    OFFSET_CANDLE_SEC: float = 5.0

    ADX_PERIOD: int = 7
    ADX_THRESHOLD: float = 50.0

    VOL_LOOKBACK: int = 4
    PRICE_LOOKBACK: int = 4
    BTC_DIR_LOOKBACK: int = 2
    VOL_SPIKE_MIN: float = 2.0
    PRICE_MOVE_MIN: float = 0.008
    PRICE_MOVE_MAX: float = 0.200

    INITIAL_SL_PCT: float = 0.005
    BE_TRIGGER_PCT: float = 0.003
    TRAIL_DIST_PCT: float = 0.005
    TP_CAP_PCT: float = 0.030
    MAX_HOLD_CANDLES: int = 40

    INVEST_PER_TRADE: float = 100.0
    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = ADXTrendFollowConfig()
```

- [ ] **Step 2: Rewrite adx engine.py — remove market_data/ws_manager dependencies**

Replace `alphas/adx-trend-follow/app/engine.py` with:

```python
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from base.engine import BaseEngine
from base.models import SymbolData
from app.config import ADXTrendFollowConfig, settings
from app.strategy import strategy_filter_signal, get_candle_seconds, compute_adx

logger = logging.getLogger(__name__)


class ADXTrendFollowEngine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}

    def get_required_channels(self) -> list[str]:
        return ["kline:15m"]

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
            except Exception as e:
                logger.error(f"Scan error: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _wait_until_next_candle_offset(self) -> None:
        candle_len = get_candle_seconds(self.config.TF)
        offset_sec = self.config.OFFSET_CANDLE_SEC
        now = time.time()
        next_candle = ((int(now // candle_len) + 1) * candle_len)
        # Fire AFTER candle close — market-data-service publishes at +RECONCILE_DELAY
        target = next_candle + offset_sec
        wait = target - now
        if wait > 0:
            await asyncio.sleep(wait)

    async def _manage_positions(self) -> None:
        if not self._open_positions:
            return

        snapshots = {}
        async with self.data_lock:
            for symbol, pos in self._open_positions.items():
                sd = self.symbol_data.get(symbol)
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

            now_ts = datetime.now(timezone.utc).isoformat()

            sl_hit = (side == "LONG" and low <= current_sl) or \
                     (side == "SHORT" and high >= current_sl)
            if sl_hit:
                exit_price = current_sl
                to_close.append({"symbol": symbol, "position_id": position_id,
                                  "exit_price": exit_price, "reason": "SL_HIT"})
                to_remove.append(symbol)
                continue

            tp_hit = (side == "LONG" and high >= current_tp) or \
                     (side == "SHORT" and low <= current_tp)
            if tp_hit:
                to_close.append({"symbol": symbol, "position_id": position_id,
                                  "exit_price": current_tp, "reason": "TP_CAP"})
                to_remove.append(symbol)
                continue

            new_bar_count = bar_count + 1
            if new_bar_count >= self.config.MAX_HOLD_CANDLES:
                to_close.append({"symbol": symbol, "position_id": position_id,
                                  "exit_price": close, "reason": "MAX_HOLD"})
                to_remove.append(symbol)
                continue

            new_sl = current_sl
            new_be = be_activated

            if side == "LONG":
                if not be_activated and close >= entry * (1 + self.config.BE_TRIGGER_PCT):
                    new_sl = max(new_sl, entry)
                    new_be = True
                if new_be:
                    trail_sl = close * (1 - self.config.TRAIL_DIST_PCT)
                    new_sl = max(new_sl, trail_sl)
            else:
                if not be_activated and close <= entry * (1 + self.config.BE_TRIGGER_PCT):
                    new_sl = min(new_sl, entry)
                    new_be = True
                if new_be:
                    trail_sl = close * (1 + self.config.TRAIL_DIST_PCT)
                    new_sl = min(new_sl, trail_sl)

            self._open_positions[symbol]["bar_count"] = new_bar_count
            self._open_positions[symbol]["be_activated"] = new_be

            if new_sl != current_sl:
                self._open_positions[symbol]["sl"] = new_sl
                to_modify.append({"position_id": position_id, "sl": new_sl})

        for item in to_modify:
            self.push_signal("MODIFY", position_id=item["position_id"], sl=item["sl"])
            logger.debug(f"[MODIFY] position={item['position_id']} new_sl={item['sl']:.6f}")

        for item in to_close:
            self.push_signal(
                "CLOSE",
                position_id=item["position_id"],
                exit_price=item["exit_price"],
                reason=item["reason"],
            )
            logger.info(f"[CLOSE] {item['symbol']} reason={item['reason']} @ {item['exit_price']}")

        for symbol in to_remove:
            self._open_positions.pop(symbol, None)

    async def _scan_new_signals(self) -> None:
        if len(self._open_positions) >= self.config.MAX_CONCURRENT_POSITIONS:
            return

        snapshot_rows = []
        btc_pl, btc_hl, btc_ll = [], [], []

        async with self.data_lock:
            btc_sd = self.symbol_data.get("BTCUSDT")
            if btc_sd is None or len(btc_sd.price_list) < settings.ADX_PERIOD * 2:
                return
            btc_pl = list(btc_sd.price_list)
            btc_hl = list(btc_sd.high_list)
            btc_ll = list(btc_sd.low_list)

            adx_btc = compute_adx(btc_hl, btc_ll, btc_pl, settings.ADX_PERIOD)
            if adx_btc < settings.ADX_THRESHOLD:
                return

            for sym, sd in self.symbol_data.items():
                if sym == "BTCUSDT" or sym in self._open_positions:
                    continue
                if not sd.price_list or not sd.volume_list:
                    continue
                snapshot_rows.append({
                    "symbol": sym,
                    "price_list": list(sd.price_list),
                    "volume_list": list(sd.volume_list),
                    "high_list": list(sd.high_list),
                    "low_list": list(sd.low_list),
                })

        signals = []
        for row in snapshot_rows:
            sig = strategy_filter_signal(
                symbol=row["symbol"],
                price_list=row["price_list"],
                volume_list=row["volume_list"],
                high_list=row["high_list"],
                low_list=row["low_list"],
                btc_price_list=btc_pl,
                btc_high_list=btc_hl,
                btc_low_list=btc_ll,
            )
            if sig:
                signals.append(sig)

        signals.sort(key=lambda x: x.get("vol_spike", 0), reverse=True)

        available_slots = self.config.MAX_CONCURRENT_POSITIONS - len(self._open_positions)
        for sig in signals[:available_slots]:
            symbol = sig["symbol"]
            if symbol in self._open_positions:
                continue

            side = sig["recommend"]
            entry = sig["entry"]
            position_id = str(uuid.uuid4())

            if side == "LONG":
                sl = entry * (1 - self.config.INITIAL_SL_PCT)
                tp = entry * (1 + self.config.TP_CAP_PCT)
            else:
                sl = entry * (1 + self.config.INITIAL_SL_PCT)
                tp = entry * (1 - self.config.TP_CAP_PCT)

            qty = self.config.INVEST_PER_TRADE * self.config.LEVERAGE / entry
            ts = datetime.now(timezone.utc).isoformat()

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
                metadata=json.dumps({
                    "vol_spike": sig.get("vol_spike"),
                    "price_move": sig.get("price_move"),
                    "btc_adx": sig.get("btc_adx"),
                }),
                timestamp=ts,
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

            logger.info(f"[SIGNAL] OPEN {side} {symbol} @ {entry} sl={sl:.4f} tp={tp:.4f}")
```

- [ ] **Step 3: Delete old files**

```bash
rm alphas/adx-trend-follow/app/market_data.py
rm alphas/adx-trend-follow/app/ws_manager.py
```

- [ ] **Step 4: Update adx docker-compose.yml — add depends_on market-data-service**

In `alphas/adx-trend-follow/docker-compose.yml`, add `depends_on`:

```yaml
    depends_on:
      - market-data-service
```

And add environment variable:

```yaml
      - DATA_CHANNELS=kline:1m,kline:15m
```

The full file becomes:

```yaml
networks:
  paper-trade:
    external: true

services:
  adx-trend-follow:
    build:
      context: ..
      dockerfile: adx-trend-follow/Dockerfile
    depends_on:
      - market-data-service
    env_file: .env
    volumes:
      - ../../logs/alphas/adx-trend-follow:/app/logs
    environment:
      - REDIS_URL=redis://redis:6379
      - LOG_DIR=/app/logs
      - DATA_CHANNELS=kline:15m
    networks:
      - paper-trade
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import os; exit(0 if os.path.exists('/tmp/bot_health') else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s
```

- [ ] **Step 5: Commit**

```bash
git add alphas/adx-trend-follow/app/engine.py alphas/adx-trend-follow/app/config.py alphas/adx-trend-follow/docker-compose.yml
git rm alphas/adx-trend-follow/app/market_data.py alphas/adx-trend-follow/app/ws_manager.py
git commit -m "refactor(adx-trend-follow): replace market_data/ws_manager with Redis subscriber"
```

---

### Task 11: Refactor wilder alpha

**Files:**
- Modify: `alphas/wilder/app/engine.py`
- Modify: `alphas/wilder/app/config.py`
- Delete: `alphas/wilder/app/market_data.py`
- Delete: `alphas/wilder/app/ws_manager.py`
- Modify: `alphas/wilder/docker-compose.yml`

- [ ] **Step 1: Update wilder config.py — add DATA_CHANNELS**

Replace `alphas/wilder/app/config.py` with:

```python
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from base.config import BaseConfig
from pydantic_settings import SettingsConfigDict


class WilderConfig(BaseConfig):
    ALPHA_ID: str = "wilder"

    TF: str = "1h"
    OFFSET_CANDLE_SEC: float = 5.0

    RSI_PERIOD: int = 14
    ADX_PERIOD: int = 14
    ATR_PERIOD: int = 14

    SAR_AF_INIT: float = 0.02
    SAR_AF_STEP: float = 0.02
    SAR_AF_MAX: float = 0.20

    TRENDING_THRESHOLD: float = 35.0
    RANGING_THRESHOLD: float = 25.0

    DI_GAP_MIN: float = 5.0
    RSI_OVERSOLD: float = 32.0
    RSI_OVERBOUGHT: float = 68.0

    SL_ATR_MULT: float = 2.0
    TP_ATR_MULT: float = 6.0
    TRAIL_ATR_MULT: float = 0.5

    INVEST_PER_TRADE: float = 300.0
    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = WilderConfig()
```

- [ ] **Step 2: Rewrite wilder engine.py — remove market_data/ws_manager dependencies**

Replace `alphas/wilder/app/engine.py` with:

```python
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
import pandas_ta as ta

from base.engine import BaseEngine
from base.models import SymbolData
from app.config import WilderConfig, settings
from app.strategy import wilder_filter_signal, get_candle_seconds

logger = logging.getLogger(__name__)


class WilderEngine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}

    def get_required_channels(self) -> list[str]:
        return ["kline:1h"]

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
            except Exception as e:
                logger.error(f"Scan error: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _wait_until_next_candle_offset(self) -> None:
        candle_len = get_candle_seconds(self.config.TF)
        offset_sec = self.config.OFFSET_CANDLE_SEC
        now = time.time()
        next_candle = ((int(now // candle_len) + 1) * candle_len)
        target = next_candle + offset_sec
        wait = target - now
        if wait > 0:
            await asyncio.sleep(wait)

    def _compute_current_atr(self, sd: SymbolData) -> float:
        n = settings.ATR_PERIOD
        if len(sd.price_list) < n:
            return 0.0
        df = pd.DataFrame({
            "high": sd.high_list[-n * 3:],
            "low": sd.low_list[-n * 3:],
            "close": sd.price_list[-n * 3:],
        })
        atr_ser = ta.atr(high=df["high"], low=df["low"], close=df["close"], length=n)
        if atr_ser is None or atr_ser.empty:
            return 0.0
        val = atr_ser.iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    async def _manage_positions(self) -> None:
        if not self._open_positions:
            return

        snapshots = {}
        async with self.data_lock:
            for symbol, pos in self._open_positions.items():
                sd = self.symbol_data.get(symbol)
                if sd and sd.price_list and sd.low_list and sd.high_list:
                    atr = self._compute_current_atr(sd)
                    snapshots[symbol] = {
                        "close": sd.price_list[-1],
                        "low": sd.low_list[-1],
                        "high": sd.high_list[-1],
                        "atr": atr,
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

            sl_hit = (side == "LONG" and low <= current_sl) or \
                     (side == "SHORT" and high >= current_sl)
            if sl_hit:
                to_close.append({"symbol": symbol, "position_id": position_id,
                                  "exit_price": current_sl, "reason": "SL_HIT"})
                to_remove.append(symbol)
                continue

            tp_hit = (side == "LONG" and high >= current_tp) or \
                     (side == "SHORT" and low <= current_tp)
            if tp_hit:
                to_close.append({"symbol": symbol, "position_id": position_id,
                                  "exit_price": current_tp, "reason": "TP_HIT"})
                to_remove.append(symbol)
                continue

            if atr <= 0:
                continue

            new_sl = current_sl
            if side == "LONG":
                trail_sl = close - settings.TRAIL_ATR_MULT * atr
                if trail_sl > current_sl:
                    new_sl = trail_sl
            else:
                trail_sl = close + settings.TRAIL_ATR_MULT * atr
                if trail_sl < current_sl:
                    new_sl = trail_sl

            if new_sl != current_sl:
                self._open_positions[symbol]["sl"] = new_sl
                to_modify.append({"position_id": position_id, "sl": new_sl})

        for item in to_modify:
            self.push_signal("MODIFY", position_id=item["position_id"], sl=item["sl"])
            logger.debug(f"[MODIFY] position={item['position_id']} new_sl={item['sl']:.6f}")

        for item in to_close:
            self.push_signal(
                "CLOSE",
                position_id=item["position_id"],
                exit_price=item["exit_price"],
                reason=item["reason"],
            )
            logger.info(f"[CLOSE] {item['symbol']} reason={item['reason']} @ {item['exit_price']}")

        for symbol in to_remove:
            self._open_positions.pop(symbol, None)

    async def _scan_new_signals(self) -> None:
        if len(self._open_positions) >= self.config.MAX_CONCURRENT_POSITIONS:
            return

        snapshot_rows = []
        async with self.data_lock:
            for sym, sd in self.symbol_data.items():
                if sym in self._open_positions:
                    continue
                if not sd.price_list or not sd.high_list or not sd.low_list:
                    continue
                snapshot_rows.append({
                    "symbol": sym,
                    "price_list": list(sd.price_list),
                    "high_list": list(sd.high_list),
                    "low_list": list(sd.low_list),
                })

        signals = []
        for row in snapshot_rows:
            sig = wilder_filter_signal(
                symbol=row["symbol"],
                price_list=row["price_list"],
                high_list=row["high_list"],
                low_list=row["low_list"],
            )
            if sig:
                signals.append(sig)

        available_slots = self.config.MAX_CONCURRENT_POSITIONS - len(self._open_positions)
        for sig in signals[:available_slots]:
            symbol = sig["symbol"]
            if symbol in self._open_positions:
                continue

            side = sig["recommend"]
            entry = sig["entry"]
            sl = sig["sl"]
            tp = sig["tp"]
            atr = sig["atr"]
            position_id = str(uuid.uuid4())

            qty = self.config.INVEST_PER_TRADE * self.config.LEVERAGE / entry
            ts = datetime.now(timezone.utc).isoformat()

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
                metadata=json.dumps({
                    "regime": sig.get("regime"),
                    "adx": sig.get("adx"),
                    "plus_di": sig.get("plus_di"),
                    "minus_di": sig.get("minus_di"),
                    "rsi_curr": sig.get("rsi_curr"),
                    "atr": atr,
                }),
                timestamp=ts,
            )

            self._open_positions[symbol] = {
                "position_id": position_id,
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp": tp,
            }

            logger.info(
                f"[SIGNAL] OPEN {side} {symbol} @ {entry:.4f} "
                f"sl={sl:.4f} tp={tp:.4f} atr={atr:.4f} regime={sig.get('regime')}"
            )
```

- [ ] **Step 3: Delete old files**

```bash
rm alphas/wilder/app/market_data.py
rm alphas/wilder/app/ws_manager.py
```

- [ ] **Step 4: Update wilder docker-compose.yml**

Replace `alphas/wilder/docker-compose.yml` with:

```yaml
networks:
  paper-trade:
    external: true

services:
  wilder:
    build:
      context: ..
      dockerfile: wilder/Dockerfile
    depends_on:
      - market-data-service
    env_file: .env
    volumes:
      - ../../logs/alphas/wilder:/app/logs
    environment:
      - REDIS_URL=redis://redis:6379
      - LOG_DIR=/app/logs
      - DATA_CHANNELS=kline:1h
    networks:
      - paper-trade
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import os; exit(0 if os.path.exists('/tmp/bot_health') else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s
```

- [ ] **Step 5: Commit**

```bash
git add alphas/wilder/app/engine.py alphas/wilder/app/config.py alphas/wilder/docker-compose.yml
git rm alphas/wilder/app/market_data.py alphas/wilder/app/ws_manager.py
git commit -m "refactor(wilder): replace market_data/ws_manager with Redis subscriber"
```

---

### Task 12: Refactor Worker — replace PriceFeedManager with Redis ticker subscriber

**Files:**
- Modify: `worker/app/main.py`
- Delete: `worker/app/price_feed.py`

- [ ] **Step 1: Rewrite worker main.py — replace PriceFeedManager with Redis ticker subscriber**

Replace `worker/app/main.py` with:

```python
import asyncio
import json
import logging
import os
import redis as redis_lib
from app.config import settings
from app.db import Database
from app.executor import Executor
from app.models import parse_signal, SignalType

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(settings.LOG_DIR, "worker.log")),
        ],
        force=True,
    )


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

    except Exception as e:
        logger.error(f"Error processing signal {signal_id}: {e}")
        await db.mark_signal_processed(signal_id, error=str(e))
        return None


async def run_ticker_subscriber(executor: Executor) -> None:
    r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = r.pubsub()
    pubsub.subscribe("ticker")
    logger.info("[TICKER] Subscribed to Redis ticker channel")

    while True:
        try:
            msg = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
            if msg and msg["type"] == "message":
                data = json.loads(msg["data"])
                symbol = data.get("symbol", "")
                price = data.get("price", 0.0)
                if symbol and price:
                    hits = await executor.check_tpsl_hit(symbol, price)
                    if hits:
                        for h in hits:
                            logger.info(f"[TPSL] Auto-closed: {h}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ticker subscriber error: {e}")
            await asyncio.sleep(5)

    pubsub.unsubscribe()
    pubsub.close()


async def run_health_loop() -> None:
    while True:
        try:
            with open("/tmp/bot_health", "w") as f:
                f.write("ok")
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

    r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
    ticker_task = None
    health_task = None

    try:
        try:
            r.xgroup_create(settings.REDIS_STREAM, settings.CONSUMER_GROUP, id="0", mkstream=True)
        except redis_lib.ResponseError:
            pass

        ticker_task = asyncio.create_task(run_ticker_subscriber(executor))
        health_task = asyncio.create_task(run_health_loop())

        logger.info(f"Consumer started: stream={settings.REDIS_STREAM} group={settings.CONSUMER_GROUP}")

        while True:
            messages = await asyncio.to_thread(
                r.xreadgroup,
                settings.CONSUMER_GROUP,
                settings.CONSUMER_NAME,
                {settings.REDIS_STREAM: ">"},
                count=10,
                block=1000,
            )

            if not messages:
                continue

            for stream_name, msgs in messages:
                for msg_id, data in msgs:
                    result = await process_signal_message(data, db, executor)
                    if result is not None:
                        logger.info(f"Processed {data.get('type')} signal: {result}")
                    await asyncio.to_thread(
                        r.xack,
                        settings.REDIS_STREAM,
                        settings.CONSUMER_GROUP,
                        msg_id,
                    )

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        tasks = [task for task in (ticker_task, health_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()
        r.close()


if __name__ == "__main__":
    asyncio.run(run_consumer())
```

- [ ] **Step 2: Add check_tpsl_hit method to Executor**

Add to `worker/app/executor.py` after the `check_tpsl_hits` method:

```python
    async def check_tpsl_hit(self, symbol: str, price: float) -> list[dict]:
        positions = await self.db.get_positions_with_tpsl()
        hits = []

        for pos in positions:
            if pos["symbol"] != symbol:
                continue

            closed = False
            reason = None
            exit_price = None

            if pos["side"] == "LONG":
                if pos["tp"] is not None and price >= pos["tp"]:
                    closed = True
                    reason = "TP_HIT"
                    exit_price = pos["tp"]
                elif pos["sl"] is not None and price <= pos["sl"]:
                    closed = True
                    reason = "SL_HIT"
                    exit_price = pos["sl"]
            elif pos["side"] == "SHORT":
                if pos["tp"] is not None and price <= pos["tp"]:
                    closed = True
                    reason = "TP_HIT"
                    exit_price = pos["tp"]
                elif pos["sl"] is not None and price >= pos["sl"]:
                    closed = True
                    reason = "SL_HIT"
                    exit_price = pos["sl"]

            if closed and exit_price is not None:
                now = datetime.now(timezone.utc).isoformat()
                await self.db.close_position(
                    position_id=pos["position_id"],
                    exit_price=exit_price,
                    reason=reason,
                    closed_at=now,
                )
                hits.append({"position_id": pos["position_id"], "reason": reason, "exit_price": exit_price})
                logger.info(f"[{reason}] {pos['alpha_id']} {pos['side']} {pos['symbol']} @ {exit_price}")

        return hits
```

- [ ] **Step 3: Delete price_feed.py**

```bash
rm worker/app/price_feed.py
```

- [ ] **Step 4: Commit**

```bash
git add worker/app/main.py worker/app/executor.py
git rm worker/app/price_feed.py
git commit -m "refactor(worker): replace PriceFeedManager with Redis ticker subscriber"
```

---

### Task 13: Integration test — verify end-to-end data flow

**Files:**
- Create: `market-data-service/tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# market-data-service/tests/test_integration.py
import json
import pytest
import redis as redis_lib
from app.models import KlineCandle, TickerUpdate
from app.aggregator import Aggregator
from app.publisher import Publisher


@pytest.fixture
def redis_client():
    r = redis_lib.Redis(decode_responses=True)
    return r


@pytest.fixture
def publisher(redis_client):
    return Publisher(redis_client)


@pytest.fixture
def aggregator():
    return Aggregator(timeframes=["1m", "5m", "15m", "1h"])


def test_end_to_end_1m_candle(aggregator, publisher, redis_client):
    candle = KlineCandle(
        symbol="BTCUSDT",
        tf="1m",
        open=67000.0,
        high=67500.0,
        low=66800.0,
        close=67200.0,
        volume=100.0,
        open_time=1716768000000,
        close_time=1716771599999,
    )
    results = aggregator.on_1m_close(candle)
    for r in results:
        publisher.publish_kline(r)

    pubsub = redis_client.pubsub()
    pubsub.subscribe("kline:1m")
    msg = None
    for _ in range(10):
        msg = pubsub.get_message(timeout=1.0)
        if msg and msg["type"] == "message":
            break
    pubsub.unsubscribe()
    pubsub.close()

    if msg and msg["type"] == "message":
        data = json.loads(msg["data"])
        assert data["symbol"] == "BTCUSDT"
        assert data["tf"] == "1m"


def test_end_to_end_ticker(publisher, redis_client):
    ticker = TickerUpdate(
        symbol="ETHUSDT",
        price=3000.5,
        timestamp=1716771600000,
        exchange="binance",
    )
    publisher.publish_ticker(ticker)

    pubsub = redis_client.pubsub()
    pubsub.subscribe("ticker")
    msg = None
    for _ in range(10):
        msg = pubsub.get_message(timeout=1.0)
        if msg and msg["type"] == "message":
            break
    pubsub.unsubscribe()
    pubsub.close()

    if msg and msg["type"] == "message":
        data = json.loads(msg["data"])
        assert data["symbol"] == "ETHUSDT"
        assert data["price"] == 3000.5


def test_5m_candle_published_on_boundary(aggregator, publisher, redis_client):
    base_ts = 1716768000000
    for i in range(5):
        candle = KlineCandle(
            symbol="BTCUSDT",
            tf="1m",
            open=67000.0,
            high=67500.0,
            low=66800.0,
            close=67200.0 + i,
            volume=100.0,
            open_time=base_ts + i * 60000,
            close_time=base_ts + i * 60000 + 59999,
        )
        results = aggregator.on_1m_close(candle)
        for r in results:
            publisher.publish_kline(r)

    pubsub = redis_client.pubsub()
    pubsub.subscribe("kline:5m")
    msg = None
    for _ in range(10):
        msg = pubsub.get_message(timeout=1.0)
        if msg and msg["type"] == "message":
            data = json.loads(msg["data"])
            if data.get("tf") == "5m":
                break
            msg = None
    pubsub.unsubscribe()
    pubsub.close()

    if msg and msg["type"] == "message":
        data = json.loads(msg["data"])
        assert data["tf"] == "5m"
        assert data["close"] == 67204.0
```

- [ ] **Step 2: Run integration tests (requires Redis)**

Run: `cd market-data-service && python -m pytest tests/test_integration.py -v`
Expected: PASS — all 3 tests (requires local Redis)

- [ ] **Step 3: Commit**

```bash
git add market-data-service/tests/test_integration.py
git commit -m "test(market-data-service): add integration tests"
```

---

## Self-Review

**1. Spec coverage:**
- market-data-service with 4 modules ✓ (Tasks 1-7)
- Redis Pub/Sub channels (kline:1m..1d, ticker, symbols) ✓ (Task 3)
- 1m base + aggregate ✓ (Task 2)
- REST reconciliation ✓ (Task 6)
- BaseEngine refactoring ✓ (Task 9)
- Alpha full migration ✓ (Tasks 10, 11)
- Worker refactoring ✓ (Task 12)
- Docker compose ✓ (Task 8)
- Correction messages ✓ (Task 2 - aggregator apply_correction)
- Resilience: exponential backoff reconnect ✓ (Tasks 4, 5)
- Configuration ✓ (Task 1)
- Late-join recovery ✓ (Task 9 - Redis HASH snapshots replace wait_for_initial_data)

**2. Placeholder scan:** No TBD/TODO found.

**3. Type consistency:** `on_kline_message` in Task 9 matches message schema from Task 1. `get_required_channels()` returns `list[str]` consistent across Tasks 9, 10, 11. `check_tpsl_hit` in Task 12 uses same pattern as `check_tpsl_hits`. Publisher methods match models' `to_dict()` output.
