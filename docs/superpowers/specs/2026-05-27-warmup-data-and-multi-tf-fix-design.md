# Warmup Data Layer & Multi-TF Fix - Design Spec

## Problem

Two issues in the current market-data-service + alpha architecture:

1. **Warmup data gap**: Alphas need enough historical bars at their primary TF to compute indicators immediately at startup (e.g. Wilder needs ~50 bars at 1h). Currently, alphas only subscribe to Redis pub/sub live feeds — they must wait for live WS messages to accumulate enough data. While Redis snapshots exist (500 candles), they may not contain the right TF or enough bars.

2. **Multi-TF bug in BaseEngine**: `on_kline_message` ignores the `tf` field in kline messages, merging all TFs into the same flat `SymbolData` lists. If an alpha subscribes to both `kline:1m` and `kline:15m`, the data gets interleaved and corrupted, with 5m/15m candles silently dropped by the dedup logic.

## Solution

### Warmup layer

Alpha sends a warmup request via Redis Stream to market-data-service at startup. MDS responds with historical candle data from its in-memory aggregator at the requested TF and bar count. Alpha populates `symbol_data` before subscribing to live feeds.

### Multi-TF storage fix

Change `symbol_data` from `dict[str, SymbolData]` (keyed by symbol) to `dict[str, dict[str, SymbolData]]` (keyed by symbol then TF). Alphas only subscribe to their primary TF channel, so no mixing occurs.

## Warmup Data Flow

```
Alpha start                              Market Data Service
    │                                          │
    │  1. XADD warmup:request                  │
    │     {alpha_id, tf, bars, symbols}        │
    │  ──────────────────────────────────────► │
    │                                          │  2. Read warmup:request stream
    │                                          │     aggregator.get_candles(sym, tf)[-bars:]
    │                                          │
    │  3. XADD warmup:response:{alpha_id}      │
    │     {symbol, tf, candle_json_array}       │
    │  ◄────────────────────────────────────── │
    │                                          │
    │  4. Parse candles → populate             │
    │     symbol_data[symbol][tf]              │
    │                                          │
    │  5. Subscribe live kline:{tf} channels   │
    │  ──────────────────────────────────────► │
    │                                          │
```

### Warmup request schema (Redis Stream `warmup:request`)

```json
{
  "alpha_id": "wilder",
  "tf": "1h",
  "bars": 50,
  "symbols": "BTCUSDT,ETHUSDT,..."
}
```

### Warmup response (Redis Stream `warmup:response:{alpha_id}`)

One message per symbol, containing a JSON array of candles:

```json
{
  "symbol": "BTCUSDT",
  "tf": "1h",
  "candles": "[{\"open_time\":1716768000000,\"open\":67000,\"high\":67500,\"low\":66800,\"close\":67200,\"volume\":12345.6},...]"
}
```

### Alpha startup sequence (new)

1. XADD `warmup:request` with tf + bars + symbols
2. XREAD `warmup:response:{alpha_id}` — block until all symbol responses received or timeout
3. Parse candles → populate `symbol_data[symbol][tf]`
4. Subscribe live `kline:{tf}` channels
5. Start `scan_loop()`

### MDS warmup handler

- Subscribe `warmup:request` stream via consumer group
- For each request: query `aggregator.get_candles(symbol, tf)[-bars:]` for each symbol
- XADD `warmup:response:{alpha_id}` for each symbol
- If aggregator doesn't have enough data yet (service just started), return what's available — alpha will fill the rest from live feeds

## Multi-TF SymbolData Storage

### Structure change

```python
# Before
self.symbol_data: dict[str, SymbolData] = {}

# After
self.symbol_data: dict[str, dict[str, SymbolData]] = {}
```

Example:
```python
engine.symbol_data["BTCUSDT"]["15m"]  # 15m candles
engine.symbol_data["BTCUSDT"]["1h"]   # 1h candles
engine.symbol_data["ETHUSDT"]["15m"]  # another symbol
```

### on_kline_message fix

```python
def on_kline_message(self, msg: dict) -> None:
    symbol = msg.get("symbol", "")
    tf = msg.get("tf", "")
    if not symbol or not tf:
        return

    if symbol not in self.symbol_data:
        self.symbol_data[symbol] = {}
    if tf not in self.symbol_data[symbol]:
        self.symbol_data[symbol][tf] = SymbolData()

    sd = self.symbol_data[symbol][tf]
    # append/correct logic same as before, operating on per-TF SymbolData
```

### Strategy reads primary TF only

Strategy code unchanged — reads from `self.symbol_data.get(symbol, {}).get(self.config.TF)`.

### get_required_channels — subscribe primary TF only

- adx: `["kline:15m"]` (drop `kline:1m`)
- wilder: `["kline:1h"]` (drop `kline:1m`)

## BaseEngine Changes

### New abstract method

```python
@abstractmethod
def _get_warmup_symbols(self) -> list[str]:
    """Return symbols to request warmup data for."""
```

### New methods

**`_request_warmup()`**: Sends XADD to `warmup:request`, then XREADGROUP from `warmup:response:{alpha_id}` to collect responses. Timeout 30s. Populates `symbol_data[symbol][tf]` via `_load_warmup_candles()`.

**`_load_warmup_candles(data: dict)`**: Parses warmup response JSON, appends candle arrays to the correct `symbol_data[symbol][tf]` SymbolData.

### Modified run() sequence

```python
async def run(self) -> None:
    # ... logging, signal handlers ...
    
    # 1. Request warmup data
    await self._request_warmup()
    
    # 2. Subscribe live feeds (primary TF only)
    sub_task = await self.subscribe_data_feeds()
    
    # 3. Verify data ready
    if len(self.symbol_data) == 0:
        self._logger.error("No data received, exiting")
        return
    
    # 4. Start scan loop
    scan_task = asyncio.create_task(self.scan_loop())
    health_task = asyncio.create_task(self._health_loop())
    # ...
```

### Symbol blacklist

Alphas can exclude specific symbols from warmup and live data processing. Blacklist is alpha-side only — MDS publishes all symbols, alpha filters locally.

**BaseConfig addition:**
```python
SYMBOL_BLACKLIST: str = ""  # comma-separated, e.g. "BTCUSDT,ETHUSDT"
```

**`_is_blacklisted(symbol)` method in BaseEngine:**
```python
def _is_blacklisted(self, symbol: str) -> bool:
    return symbol in self._blacklist

# In __init__:
self._blacklist: set[str] = {
    s.strip().upper() for s in self.config.SYMBOL_BLACKLIST.split(",") if s.strip()
}
```

**Filtering points:**

1. **`_get_warmup_symbols()`**: Each alpha's implementation subtracts `self._blacklist` before sending warmup request
2. **`on_kline_message()`**: Skip blacklisted symbols — `if self._is_blacklisted(symbol): return`
3. **`_load_warmup_candles()`**: Skip blacklisted symbols — `if self._is_blacklisted(symbol): return`

MDS is unaware of blacklists. It publishes all symbols on `kline:{tf}` channels. Alpha simply ignores messages for blacklisted symbols. This avoids any coordination between MDS and alphas on symbol filtering.

### BaseConfig addition (all new fields)

```python
WARMUP_BARS: int = 50
SYMBOL_BLACKLIST: str = ""  # comma-separated, e.g. "BTCUSDT,ETHUSDT"
```

## Alpha Changes

### ADX-trend-follow

- `get_required_channels()` → `["kline:15m"]` only
- `_get_warmup_symbols()` → calls `get_binance_perp_symbols()` (helper moved to `alphas/base/symbol_utils.py` since `market_data.py` was deleted), then subtracts `self._blacklist`
- All `self.symbol_data.get(symbol)` → `self.symbol_data.get(symbol, {}).get(self.config.TF)`
- Applies to: `_manage_positions`, `_scan_new_signals` (BTC + scan symbols)

### Wilder

- `get_required_channels()` → `["kline:1h"]` only
- `_get_warmup_symbols()` → calls `get_top_n_binance_perps(n)` (helper moved to `alphas/base/symbol_utils.py`), then subtracts `self._blacklist`
- All `self.symbol_data.get(symbol)` → `self.symbol_data.get(symbol, {}).get(self.config.TF)`
- Remove `fetch_closed_candles_batch` call from scan_loop — reconciliation handled by MDS

### Worker

No changes. Worker subscribes to `ticker` channel only, doesn't use `symbol_data`.

## MDS Warmup Handler

New module `market-data-service/app/warmup_handler.py`:

- Runs as an async task alongside kline/ticker/reconciler tasks
- Subscribes to `warmup:request` Redis Stream via consumer group
- For each request:
  1. Parse `alpha_id`, `tf`, `bars`, `symbols`
  2. For each symbol: `aggregator.get_candles(symbol, tf)[-bars:]`
  3. Serialize candles to JSON array
  4. XADD to `warmup:response:{alpha_id}` with `{symbol, tf, candles}`
- If aggregator has fewer candles than requested, return what's available
- ACK each request after processing

### Startup integration

In `main.py`, add warmup handler task:
```python
tasks.append(asyncio.create_task(
    warmup_handler.run(aggregator, r)
))
```

Run after initial data load so aggregator is populated.

## Resilience

| Failure mode | Handling |
|-------------|----------|
| Warmup request timeout (30s) | Alpha logs warning, starts with empty data, fills from live feeds |
| MDS not started yet | Alpha warmup request sits in stream until MDS consumes it — alpha times out after 30s, proceeds with live data |
| Partial warmup response | Alpha uses what it received, fills gaps from live feeds |
| MDS restart | Aggregator re-seeds from REST, warmup handler re-processes any pending requests |
