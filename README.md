# Paper Trade System

Paper Trade System runs multiple strategy processes against shared market data,
simulates executions, stores positions and trades in SQLite, and presents results
through a web dashboard.

It is a simulation environment. It does not submit live exchange orders.

## Architecture

```text
Market Data Service
  | kline / price_alert / snapshots / warm-up
  v
Independent alpha containers
  | OPEN / MODIFY / CLOSE / REGISTER_COLUMNS
  v
Redis Stream: paper-signals
  |
  v
Paper execution worker
  |
  v
SQLite: positions, trades, signals, alpha metadata
  |
  v
Next.js dashboard and JSON/SSE APIs
```

The system is separated into three responsibilities:

- **Alphas** consume MDS data, calculate strategy state, manage simulated
  position intent, and publish signals.
- **Worker** consumes the signal stream, applies execution rules and slippage,
  and writes the transactional state to SQLite.
- **Web** reads SQLite and MDS price alerts to display performance, positions,
  trades, equity, and live position ticks.

## Requirements

- Docker Engine with Docker Compose v2
- GNU Make
- A running Market Data Service stack
- The external Docker network named `market-data`

For local Python tests:

- Python 3.12+
- Dependencies from the relevant component requirements file

## Quick Start

Start MDS first:

```bash
cd ../market-data-service
cp .env.example .env
make up
make health
```

Then start the paper-trade core:

```bash
cd ../paper-trade-system
make up
make health
```

Start all alphas listed in `REGISTERED_ALPHAS`:

```bash
make alphas-up
make alphas-health
```

Default local endpoints:

| Service | Address |
|---|---|
| Dashboard | `http://localhost:8097` |
| Paper Redis | `localhost:6382` |
| Paper Redis network alias | `paper-redis` |
| MDS Redis network alias | `mds-redis` |
| SQLite database | `data/paper-trade.db` |

## Configuration

The root `.env` controls Compose, the dashboard, worker overrides, and the MDS
connection passed to alpha containers.

Minimal root `.env`:

```dotenv
WEB_PORT=8097
REDIS_PORT=6382
LOG_LEVEL=INFO

REGISTERED_ALPHAS=alpha-1-v5b,alpha-1-q1
DASHBOARD_CACHE_MS=1000
SIGNAL_RETENTION_DAYS=0

SLIPPAGE_PCT=0.05
DUPLICATE_POSITION_POLICY=reject
REDIS_READ_COUNT=100
REDIS_BLOCK_MS=1000

MDS_REDIS_URL=redis://mds-redis:6379
MDS_EXCHANGE=binance
```

### Root Settings

| Variable | Default | Description |
|---|---:|---|
| `WEB_PORT` | `8097` | Dashboard host port |
| `REDIS_PORT` | `6382` | Paper Redis host port |
| `REGISTERED_ALPHAS` | empty | Comma-separated alpha directories managed by Make |
| `MDS_REDIS_URL` | `redis://mds-redis:6379` | Shared MDS Redis endpoint |
| `MDS_EXCHANGE` | `binance` | Default MDS exchange passed to alphas |
| `SLIPPAGE_PCT` | `0.05` | Worker slippage input; applied as `price * value / 1000` |
| `DUPLICATE_POSITION_POLICY` | `reject` | Behavior when an alpha opens the same symbol twice |
| `REDIS_READ_COUNT` | `100` | Maximum signal entries read per worker batch |
| `REDIS_BLOCK_MS` | `1000` | Signal-stream blocking read timeout |
| `SIGNAL_RETENTION_DAYS` | `0` | Signal audit retention; `0` disables pruning |
| `DASHBOARD_CACHE_MS` | `1000` | Dashboard API cache duration |
| `LOG_LEVEL` | `INFO` | Core service log level |

Each alpha also has its own `.env`, strategy config class, optional
`config.toml`, blacklist, and data files.

Common alpha settings are defined in `alphas/base/config.py`:

| Variable | Default | Description |
|---|---:|---|
| `ALPHA_ID` | empty | Unique alpha identifier |
| `REDIS_URL` | `redis://localhost:6379` | Paper Redis endpoint for signals |
| `REDIS_STREAM` | `paper-signals` | Signal stream |
| `MDS_REDIS_URL` | empty | External MDS Redis endpoint |
| `MDS_EXCHANGE` | empty | Exchange namespace used by MDS |
| `WARMUP_BARS` | `50` | Historical candles required before trading |
| `INITIAL_DATA_TIMEOUT_SEC` | `300` | Maximum warm-up wait for large queued requests |
| `DATA_MAX_CANDLES` | `1000` | In-memory candles retained per symbol/timeframe |
| `INVEST_PER_TRADE` | `100` | Strategy capital allocation input |
| `LEVERAGE` | `10` | Strategy leverage metadata |
| `MAX_CONCURRENT_POSITIONS` | `50` | Strategy position limit |
| `FEE_PCT` | `0.0005` | Fee fraction stored with new positions |
| `PRICE_ALERT_STALE_SEC` | `15` | Price-alert freshness threshold |

## Startup Order

The expected startup order is:

1. Start MDS and its Redis instance.
2. Start the paper-trade core with `make up`.
3. Start alpha containers with `make alphas-up`.

`make alphas-up` requires the external `market-data` network. It injects
`MDS_REDIS_URL` and `MDS_EXCHANGE` into each alpha Compose project.

## Alpha Runtime

All current strategies inherit from `alphas/base/engine.py`.

The base engine provides:

- Redis and MDS connection recovery.
- Snapshot-first warm-up for requests requiring at most 500 bars.
- Durable MDS warm-up stream fallback.
- Candle upsert and correction handling.
- Stale-data detection and runtime states.
- Price-alert subscription synchronization for open positions.
- Signal publishing and dashboard-column registration.
- Periodic scan, position-management, health, and subscription tasks.

Runtime states:

```text
STARTING -> WARMING_UP -> LIVE
                         |
                         v
                       STALE
                         |
                         v
                    RECOVERING -> LIVE
```

An alpha should not open new positions while its runtime state is stale or
recovering. Existing positions can still be managed according to the strategy's
risk logic.

Current alpha directories:

- `adx-trend-follow`
- `alpha-1-bangoc`
- `alpha-1-q1`
- `alpha-1-q2`
- `alpha-1-q3`
- `alpha-1-v5b`
- `alpha-1-v5b-5pct`
- `wilder`

Only names included in `REGISTERED_ALPHAS` are started by `make alphas-up`.

## Signal Contract

Alphas publish signals to the Redis Stream:

```text
paper-signals
```

Every signal includes:

```text
type
alpha_id
signal_id
timestamp
```

Supported signal types:

### OPEN

```json
{
  "type": "OPEN",
  "alpha_id": "alpha-1",
  "signal_id": "unique-id",
  "symbol": "BTCUSDT",
  "side": "LONG",
  "entry": "70000",
  "qty": "0.01",
  "tp": "72000",
  "sl": "69000",
  "leverage": "10",
  "exchange": "binance",
  "fee_pct": "0.000357",
  "timestamp": "2026-06-04T00:00:00+00:00"
}
```

### MODIFY

Updates `tp` or `sl` for an existing `position_id`. Trailing stops cannot move
against the position.

### CLOSE

Closes an existing `position_id`, records the exit reason, applies configured
slippage, calculates fees and PnL, and moves the row from `positions` to
`trades`. An optional `qty` closes only that amount, records a trade leg, and
keeps the remaining quantity open.

### REGISTER_COLUMNS

Registers strategy-specific metadata columns for the dashboard.

The worker logs every signal in SQLite and acknowledges the Redis Stream entry
after processing. Processing errors are recorded in the `signals.error` column.

## Execution Model

The worker is a deterministic paper executor:

- Applies adverse slippage on opens and closes.
- Rejects duplicate open positions per `alpha_id` and symbol when
  `DUPLICATE_POSITION_POLICY=reject`.
- Prevents trailing stops from moving against an open position.
- Calculates gross PnL, fees, net PnL, PnL percentage, and duration.
- Preserves open metadata and nests close audit metadata under `close`.

By default, alphas manage TP/SL using MDS `price_alert` data.
`ENABLE_WORKER_TPSL_AUTO_CLOSE` is disabled by default.

## Database

SQLite runs in WAL mode at `data/paper-trade.db`.

Main tables:

| Table | Purpose |
|---|---|
| `alphas` | Registered strategy identities and status |
| `positions` | Current open simulated positions |
| `trades` | Closed positions and calculated performance |
| `signals` | Signal audit log and processing errors |
| `alpha_columns` | Strategy-specific dashboard metadata columns |

Do not delete the database while the worker or web service is running.

Useful queries:

```bash
make db-alphas
make db-trades ALPHA=alpha-1-v5b
make db-summary ALPHA=alpha-1-v5b
make db-open ALPHA=alpha-1-v5b
make db-symbols ALPHA=alpha-1-v5b
make db-csv ALPHA=alpha-1-v5b
```

## Dashboard APIs

The web service exposes:

| Endpoint | Description |
|---|---|
| `GET /api/dashboard` | Combined dashboard summary |
| `GET /api/alphas` | All registered alphas |
| `GET /api/alphas?id={alpha_id}` | Single alpha |
| `GET /api/positions?alpha_id={alpha_id}` | Open positions |
| `GET /api/trades?alpha_id={alpha_id}` | Paginated trades |
| `GET /api/trades?alpha_id={alpha_id}&stats=1` | Alpha trade statistics |
| `GET /api/equity?alpha_id={alpha_id}` | Alpha equity curve |
| `GET /api/equity?alphas=a,b` | Compared equity curves |
| `GET /api/columns?alpha_id={alpha_id}` | Registered dashboard columns |
| `GET /api/position-ticks?alpha_id={alpha_id}` | Live MDS price-alert SSE stream |
| `GET /api/events` | Dashboard heartbeat SSE stream |

## Managing Alphas

```bash
# Start or stop all configured alphas
make alphas-up
make alphas-down

# Manage one alpha
make alpha-up ALPHA=alpha-1-v5b
make alpha-down ALPHA=alpha-1-v5b
make alpha-restart ALPHA=alpha-1-v5b
make alpha-logs ALPHA=alpha-1-v5b

# Inspect all alpha Compose projects
make alphas-ps
make alphas-health
```

### Adding an Alpha

A typical alpha directory contains:

```text
alphas/my-alpha/
  app/
    config.py
    engine.py
    strategy.py
  .env
  config.toml
  docker-compose.yml
  Dockerfile
  main.py
  requirements.txt
```

Implementation checklist:

1. Inherit from `BaseEngine`.
2. Define a unique `ALPHA_ID`.
3. Implement `get_required_channels`, `_get_warmup_symbols`, `scan_loop`,
   `_manage_positions`, and `_has_open_positions`.
4. Publish `OPEN`, `MODIFY`, and `CLOSE` signals through `self.push_signal`.
5. Join both external Docker networks: `paper-trade` and `market-data`.
6. Mount required config, blacklist, or data files.
7. Add the directory name to root `REGISTERED_ALPHAS`.

## Core Operations

```bash
make up
make down
make restart
make build
make logs
make logs-tail
make ps
make health
```

`make clean` removes the paper Redis volume and deletes
`data/paper-trade.db`. Use it only when a full local reset is intended.

## Testing

Worker tests:

```bash
cd worker
python -m pytest tests -q
```

Shared alpha-engine tests:

```bash
PYTHONPATH=alphas python -m pytest alphas/base/tests -q
```

Dashboard validation:

```bash
cd web
npm install
npm run build
```

## Deployment

Deploy core and all configured alphas:

```bash
make deploy SERVER=root@example-host
```

Deploy only Redis, worker, and web:

```bash
make deploy-core SERVER=root@example-host
```

Deploy one alpha:

```bash
make deploy-alpha ALPHA=alpha-1-v5b SERVER=root@example-host
```

Deployment helpers:

```bash
make deploy-logs SERVER=root@example-host
make deploy-ps SERVER=root@example-host
make deploy-db-recover SERVER=root@example-host
```

Deployment archives include `.env` files. Treat them as sensitive.

## Project Layout

```text
alphas/
  base/               Shared runtime, models, indicators, and signal publisher
  <alpha-name>/       Independent strategy application and Compose project
worker/
  app/                Signal consumer, executor, and SQLite data layer
  tests/              Worker tests
web/
  src/app/            Next.js dashboard and API routes
data/                 SQLite database
logs/                 Core and alpha logs
docker-compose.yml    Paper Redis, worker, and web
Makefile              Local operations, alpha management, deploy, and DB tools
```

## Troubleshooting

### Missing `market-data` network

Start MDS first:

```bash
cd ../market-data-service
make up
```

Then verify:

```bash
docker network inspect market-data
```

### Alphas restart repeatedly

```bash
make alphas-ps
make alpha-logs ALPHA=alpha-1-v5b
make health
```

Check that both `paper-redis` and `mds-redis` resolve inside the alpha container,
and that MDS has completed its initial data load.

### Warm-up is slow

Large alpha startup waves are queued by MDS to protect exchange rate limits.
Alphas wait up to `INITIAL_DATA_TIMEOUT_SEC`, use fresh snapshots first, and
enter `STALE` when required data is incomplete.

### Database health

```bash
make health
make deploy-db-recover SERVER=root@example-host
```

Use `deploy-db-reset` only when losing server trade history is acceptable.

### Signals are not processed

```bash
docker compose exec redis redis-cli XINFO GROUPS paper-signals
docker compose logs --tail=200 worker
```

Inspect the `signals` table for parsing or execution errors.

## Safety and Limitations

- This is a paper-trading system, not a live execution engine.
- Redis Pub/Sub market-data messages are transient; alphas recover using
  snapshots and warm-up requests.
- Simulated fills use a configurable slippage model and do not reproduce an
  exchange order book.
- The worker signal consumer does not currently reclaim stale pending signal
  entries after a worker crash. Inspect the stream consumer group during
  recovery incidents.
