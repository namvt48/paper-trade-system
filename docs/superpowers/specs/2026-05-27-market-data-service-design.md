# Market Data Service - Design Spec

## Problem

The paper-trade-system has 3 independent consumers each opening their own connections to Binance:

1. **adx-trend-follow alpha** — WS `@kline_15m` + REST historical klines
2. **wilder alpha** — WS `@kline_1h` + REST historical klines + REST reconciliation
3. **Worker** — WS `@ticker` (Binance/OKX/KuCoin) for TP/SL checks

~120 symbols overlap between the two alphas. Each alpha creates its own `AsyncClient`, own WebSocket connections, own in-memory `symbol_data`. No shared data infrastructure exists. Adding a new alpha duplicates all of this.

## Solution

A dedicated `market-data-service` container that is the single source of market data. It connects to Binance once, subscribes to `@kline_1m` streams, aggregates into higher timeframes in-memory, and publishes candle close events via Redis Pub/Sub. Alphas and worker subscribe to Redis channels instead of connecting to exchanges directly.

## Architecture

```
                         ┌──────────────────────────────────┐
                         │     market-data-service          │
                         │                                  │
  Binance WS             │  ┌────────────┐  ┌───────────┐  │
  @kline_1m ─────────────┤  │ KlineFeed  │  │ TickerFeed│  │
  @ticker   ─────────────┤  │ (1m OHLCV) │  │ (prices)  │  │
                         │  └─────┬──────┘  └─────┬─────┘  │
                         │        │               │         │
                         │  ┌─────▼──────┐        │         │
                         │  │ Aggregator │        │         │
                         │  │ 1m→5m→1h.. │        │         │
                         │  └─────┬──────┘        │         │
                         │        │               │         │
  Binance REST           │  ┌─────▼───────────────▼──────┐  │
  (periodic reconcile) ──┤  │     Redis Publisher         │  │
                         │  │  kline:1m kline:5m ...     │  │
                         │  │  kline:15m kline:1h ...    │  │
                         │  │  ticker                     │  │
                         │  └────────────────────────────┘  │
                         └──────────────────────────────────┘
                                          │
                              Redis Pub/Sub
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    │                     │                       │
              ┌─────▼─────┐        ┌──────▼──────┐        ┌──────▼──────┐
              │  adx alpha │        │ wilder alpha│        │   Worker    │
              │ sub: 15m   │        │ sub: 1h     │        │ sub: ticker │
              │ sub: 1m    │        │ sub: 1m     │        │ (TP/SL)     │
              └────────────┘        └─────────────┘        └─────────────┘
```

## Components

### market-data-service

Python async service with 4 modules:

| Module | Responsibility |
|--------|---------------|
| `kline_feed.py` | Subscribe `@kline_1m` WS, parse confirmed 1m OHLCV, maintain in-memory `symbol_data` |
| `ticker_feed.py` | Subscribe `@ticker` WS, maintain `_prices` dict |
| `aggregator.py` | On 1m close, rollup into higher TFs (5m/15m/1h/4h/1d), publish to Redis |
| `reconciler.py` | After TF candle close, REST fetch 2 klines to verify/fix in-memory data |

**Startup sequence:**
1. Load symbol universe from config or fetch Binance exchangeInfo
2. REST load N 1m candles history per symbol → seed in-memory store
3. Aggregate 1m history into higher TFs in-memory (no extra REST calls)
4. Start WS connections: `@kline_1m` + `@ticker`
5. Start reconciler task
6. Start Redis publisher

**Kline WS handling (1m):**
- Only process confirmed closes (`k["x"] == True`), discard partial ticks
- `asyncio.Queue(1000)` buffer between WS recv and processing
- Each 1m close → update in-memory → trigger aggregator → publish `kline:1m`
- Batch symbols: 150 per WS connection

**Aggregator logic:**
```
On 1m close at time T:
  Is T a 5m boundary?  → rollup last 5 1m candles → publish kline:5m
  Is T a 15m boundary? → rollup last 15 1m candles → publish kline:15m
  Is T a 1h boundary?  → rollup last 60 1m candles → publish kline:1h
  Is T a 4h boundary?  → rollup last 240 1m candles → publish kline:4h
  Is T a 1d boundary?  → rollup last 1440 1m candles → publish kline:1d
```

Rollup rules: `open = first.open`, `high = max(all.high)`, `low = min(all.low)`, `close = last.close`, `volume = sum(all.volume)`.

**Reconciler:**
- After each 15m close: REST fetch 2 klines at 15m TF → verify in-memory
- After each 1h close: REST fetch 2 klines at 1h TF → verify in-memory
- On mismatch: fix in-memory data, re-publish corrected candle with `correction: true`
- Semaphore(25) for REST concurrency

### Redis Pub/Sub Channels

```
kline:1m      → candle close event (JSON)
kline:5m      → candle close event
kline:15m     → candle close event
kline:30m     → candle close event
kline:1h      → candle close event
kline:4h      → candle close event
kline:1d      → candle close event
ticker        → price update event
symbols       → active symbol universe list
```

**Kline message schema:**
```json
{
  "symbol": "BTCUSDT",
  "tf": "1h",
  "open": 67000.0,
  "high": 67500.0,
  "low": 66800.0,
  "close": 67200.0,
  "volume": 12345.6,
  "open_time": 1716768000000,
  "close_time": 1716771599999,
  "confirmed": true,
  "correction": false
}
```

**Ticker message schema:**
```json
{
  "symbol": "BTCUSDT",
  "price": 67200.5,
  "timestamp": 1716771600000,
  "exchange": "binance"
}
```

**Correction messages** use the same channel and schema with `correction: true`. Alphas overwrite the matching candle in `symbol_data` instead of appending.

### Alpha refactoring

**BaseEngine changes:**
- Remove abstract methods: `load_initial_data()`, `create_ws_tasks()`
- Add abstract method: `get_required_channels() -> list[str]` — returns Redis channels the alpha needs
- Add method: `subscribe_data_feeds()` — creates Redis subscriber for required channels
- Redis subscriber updates `symbol_data` on each message (same dict interface)
- `scan_loop()` unchanged — still reads from `symbol_data`

**Files removed from each alpha:**
- `market_data.py` — no longer needed (no REST/WS code in alpha)
- `ws_manager.py` — replaced by Redis subscriber

**Files kept with modifications:**
- `engine.py` — simplified, `subscribe_data_feeds()` replaces `load_initial_data()` + `create_ws_tasks()`
- `strategy.py` — unchanged (reads from `symbol_data` as before)
- `config.py` — add `data_channels` field

**Alpha startup sequence (new):**
1. Connect to Redis
2. Call `subscribe_data_feeds()` — subscribe to required channels
3. Wait for initial data: poll `symbol_data` until it has entries for at least 50% of the expected symbol universe, with a 30s timeout. If timeout, log warning and proceed with partial data.
4. Start `scan_loop()`

### Worker refactoring

- Remove `PriceFeedManager` class and `price_feed.py`
- Worker subscribes to Redis `ticker` channel
- Maintain `self._prices` dict from Redis messages (same interface for executor)
- Remove Binance/OKX/KuCoin WS code — all ticker data from one source
- Dynamic subscription management: service publishes all tickers, worker filters locally by symbols with open positions

## Resilience

| Failure mode | Handling |
|-------------|----------|
| WS disconnect | Exponential backoff reconnect (1s→2s→4s→...→60s max) |
| WS message loss | Reconciler catches mismatch at next candle close |
| Service crash/restart | REST re-seed from scratch. Alphas detect Redis disconnect → pause scan_loop until reconnect |
| Redis down | Service and alphas reconnect. WS messages buffered in asyncio.Queue |
| Reconciler mismatch | Fix in-memory, re-publish with `correction: true` |

## Configuration

**market-data-service config (YAML):**
```yaml
exchange: binance
symbol_mode: auto          # auto = fetch from exchange, manual = from list
symbols: []                # only if symbol_mode: manual
timeframes: [1m, 5m, 15m, 30m, 1h, 4h, 1d]
history_candles: 500       # 1m candles to load at startup
ws_batch_size: 150
rest_semaphore: 25
reconcile_tfs: [15m, 1h]
reconcile_delay: 5         # seconds after candle close to reconcile
redis_url: redis://redis:6379
```

**Alpha config changes:**
```yaml
timeframe: 15m             # unchanged
redis_url: redis://redis:6379
data_channels: [kline:1m, kline:15m]  # NEW
```

## Multi-exchange extensibility

Currently Binance only. `ticker_feed.py` and `kline_feed.py` use abstract base classes with per-exchange implementations. Adding an exchange means adding an implementation class without changing aggregator or publisher.

The `exchange` config field selects which implementation to use. Future: support multiple exchanges simultaneously with separate feed instances publishing to exchange-prefixed channels (e.g. `kline:1m:okx`).

## Impact summary

| Metric | Before | After |
|--------|--------|-------|
| Binance WS connections | ~6-7 (2 alpha kline + 1 worker ticker + duplicates) | 2-3 (1 kline batch + 1 ticker) |
| REST calls at startup | 2x (each alpha loads independently) | 1x (service loads once) |
| REST reconciliation | Only wilder, duplicated per alpha | Centralized, all TFs |
| Code duplication | market_data.py, ws_manager.py copied per alpha | Removed from alphas |
| New alpha effort | Copy WS/REST/market_data boilerplate | Add `data_channels` to config |
| Worker ticker connections | 3 separate (Binance/OKX/KuCoin) | 1 Redis subscription |
