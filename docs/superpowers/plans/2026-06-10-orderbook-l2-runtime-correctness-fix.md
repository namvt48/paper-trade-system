# Order Book L2 - Paper-Trade Runtime Correctness Fix Plan

> **For agentic workers:** Execute task-by-task. Write or update the focused tests before
> changing production code. Keep paper-trade signal transport and MDS market-data transport
> as separate dependencies throughout the implementation.

**Goal:** Make the existing order-book integration work end-to-end in the deployed system,
preserve correct fill semantics across all close paths, and make outages visible without
removing the fixed-percentage fallback.

**Current state:** The L2 calculation and worker hardening logic are implemented and covered
by focused unit tests, but the deployed worker sends the MDS RPC, order-book subscription,
`ob_exec`, and ticker traffic through the paper-trade Redis connection. MDS listens on a
different Redis instance, so the integration currently falls back instead of using L2.

**Architecture:** Keep two explicit Redis boundaries:

- `REDIS_URL`: paper-trade Redis only, used for `paper-signals` and consumer-group ACKs.
- `MDS_REDIS_URL`: MDS Redis only, used for ticker, `ob_exec`, `orderbook:subscribe:*`, and
  `orderbook:slip:*`.

The worker owns both connections. Failure of MDS Redis degrades fills to fixed-percentage
and disables worker-side realtime TP/SL pricing, but must not stop signal consumption.
Failure of paper Redis is a worker-health failure because signals cannot be consumed.

**Phase-1 exchange rule:** L2 is Binance-only. The worker must immediately use fixed-pct for
unsupported exchanges and must only subscribe Binance positions to Binance order-book
channels. Do not send unsupported-exchange requests and wait for guaranteed timeouts.

**Depends on:**

- `market-data-service/docs/superpowers/plans/2026-06-09-orderbook-l2-mds.md`
- `paper-trade-system/docs/superpowers/plans/2026-06-09-orderbook-l2-papertrade.md`

**Primary findings covered by this plan:**

1. Worker and MDS order-book components use different Redis instances.
2. Worker is not attached to the `market-data` Docker network.
3. Worker ticker fallback subscribes `ticker`, while MDS publishes `ticker:{exchange}`.
4. Alpha-managed CLOSE signals can apply fixed-pct again to an already executable bid/ask.
5. Worker order-book sync is not filtered by exchange.
6. Worker auto TP/SL loses price provenance between trigger selection and fill fallback.
7. Existing healthchecks can report healthy from a stale `/tmp/bot_health` file.
8. Redis services stay exited after code 137 because they have no restart policy.
9. Order-book runtime metrics do not yet expose latency, cache-hit, shallow-book, and worker
   fallback behavior needed to prove that L2 is actually being used.
10. Worker full test suite is not isolated from `worker/.env`.
11. `SLIPPAGE_PCT` defaults disagree: worker code defaults to `0.5`, while compose, `.env`,
    and tests use `0.05`, creating a possible 10x fallback difference outside compose.

**Already fixed and retained:** stale/zero `ob_exec` guard, per-position resolver isolation,
RPC circuit breaker, request-list cap, bounded event-driven subscribe publish, CLOSE qty
guard, and worker-auto-path double-slippage guard.

**Baseline observed on 2026-06-10:**

- Focused MDS order-book suite: `38 passed`.
- Full worker suite: `102 passed, 1 failed`; the failure is default-config testing polluted
  by `worker/.env`.
- Both Redis containers were `Exited (137)`, while MDS and worker containers still reported
  healthy and repeatedly failed to resolve `redis:6379`.

**Non-goals:**

- Implementing L2 adapters for OKX, KuCoin, BingX, or Bybit.
- Making order-book imbalance an alpha entry signal.
- Removing fixed-pct fallback.
- Making worker auto TP/SL the default; alpha-managed TP/SL remains supported.

**Conventions:**

- Paper worker tests: `cd paper-trade-system/worker && python -m pytest ...`
- Alpha base tests: `cd paper-trade-system/alphas && PYTHONPATH=. python -m pytest base/tests ...`
- MDS tests: `cd market-data-service && python -m pytest ...`
- Docker verification uses both compose projects and the external `market-data` network.
- Commit after each task.

---

## Target Data Flow

```text
Alpha
  |
  | XADD paper-signals
  v
paper Redis <---------------- Worker signal consumer / ACK
                                  |
                                  | separate MDS Redis client
                                  v
MDS Redis <----> MDS
  |              - orderbook:slip:req/resp
  |              - orderbook:subscribe
  |              - ob_exec
  |              - ticker:{exchange}
  |
  +-----------> Worker order-book and ticker subscribers
```

The worker must never enqueue an MDS RPC request on paper Redis.

---

### Task 1: Add explicit paper-Redis and MDS-Redis configuration

**Files:**

- Modify: `paper-trade-system/worker/app/config.py`
- Modify: `paper-trade-system/worker/tests/test_config.py`
- Modify: `paper-trade-system/worker/.env`
- Modify: `paper-trade-system/docker-compose.yml`

**Implementation:**

- Keep `REDIS_URL` as the backward-compatible paper Redis setting.
- Add `MDS_REDIS_URL: str = ""`.
- Replace singular capability assumptions with
  `ORDERBOOK_SUPPORTED_EXCHANGES: str = "binance"`.
- Add a helper returning a normalized supported-exchange set.
- Keep `ORDERBOOK_EXCHANGE` temporarily only if another code path still requires it; remove
  it after all callers use the supported-exchange set.
- Add a startup validation helper:
  - `REDIS_URL` is always required.
  - `MDS_REDIS_URL` is required when `ENABLE_ORDERBOOK_SLIPPAGE=True` or
    `ENABLE_WORKER_TPSL_AUTO_CLOSE=True`.
  - Do not require the URLs to differ, because a deliberate single-Redis development setup
    is valid.
- Standardize the code default for `SLIPPAGE_PCT` to `0.05`, matching the current compose
  default, worker `.env`, and existing expected behavior. Keep the existing per-mille
  interpretation documented in `fill.py`.
- In Docker compose, explicitly set:
  - `REDIS_URL=redis://paper-redis:6379`
  - `MDS_REDIS_URL=redis://mds-redis:6379`
- Add the worker to both `paper-trade` and `market-data` networks.

**Tests first:**

- `Settings(_env_file=None)` returns code defaults without reading `worker/.env`.
- Supported exchanges parse whitespace, duplicates, and case consistently.
- Validation rejects enabled L2 with an empty `MDS_REDIS_URL`.
- Validation permits disabled L2 with an empty `MDS_REDIS_URL`.
- Code, compose, `.env`, and tests agree on the `SLIPPAGE_PCT=0.05` default.

**Verification:**

```bash
cd paper-trade-system/worker
python -m pytest tests/test_config.py -v
docker compose -f ../docker-compose.yml config
```

**Acceptance:**

- Worker compose config contains two distinct Redis URLs and both Docker networks.
- Full test runs no longer depend on the contents of `worker/.env`.

**Commit:**

```bash
git commit -m "fix(paper-trade): define separate paper and MDS Redis endpoints"
```

---

### Task 2: Introduce separate Redis connectors and lifecycle ownership

**Files:**

- Create: `paper-trade-system/worker/app/redis_clients.py`
- Modify: `paper-trade-system/worker/app/main.py`
- Create: `paper-trade-system/worker/tests/test_redis_clients.py`
- Modify: `paper-trade-system/worker/tests/test_main.py`

**Implementation:**

- Move Redis connection/retry logic out of `main.py`.
- Provide explicit connectors:
  - `connect_paper_redis()`
  - `make_mds_redis_client()` for the non-blocking/lazy command client used by RPC and sync.
  - `connect_mds_redis()` for long-running subscribers that reconnect independently.
- Include the dependency name and URL host in connection logs without logging credentials.
- In `run_consumer()`:
  - Create `paper_redis` for stream group creation, `XREADGROUP`, and `XACK`.
  - Create `mds_redis` only when an MDS-backed feature is enabled.
  - Construct `SlippageClient` with `mds_redis`.
  - Pass an MDS connector to ticker and `ob_exec` subscribers.
  - Pass `mds_redis` to order-book sync and event-driven subscribe.
  - Close both clients during shutdown.
- MDS Redis connection loss after startup must not block paper signal processing:
  - RPC client already falls back via timeout/circuit breaker.
  - Subscriber/sync tasks reconnect independently.
  - Do not put the retrying `connect_mds_redis()` in a blocking path before the paper
    consumer loop starts.

**Tests first:**

- A fake paper Redis receives stream operations but no `orderbook:*` keys.
- A fake MDS Redis receives `orderbook:*` operations but no `paper-signals` operations.
- Worker can start signal processing while the fake MDS connector is unavailable.
- Shutdown closes both clients exactly once.

**Verification:**

```bash
cd paper-trade-system/worker
python -m pytest tests/test_redis_clients.py tests/test_main.py tests/test_main_fill_wiring.py -v
```

**Acceptance:**

- There is no production call site where an order-book function receives `paper_redis`.
- MDS outage does not stop the paper signal loop.

**Commit:**

```bash
git commit -m "fix(paper-trade): route MDS traffic through dedicated Redis client"
```

---

### Task 3: Correct ticker safety-net subscription and freshness

**Files:**

- Modify: `paper-trade-system/worker/app/main.py`
- Modify: `paper-trade-system/worker/app/config.py`
- Modify: `paper-trade-system/worker/tests/test_ticker_cache.py`
- Create: `paper-trade-system/worker/tests/test_ticker_subscriber.py`

**Implementation:**

- Subscribe to `ticker:{exchange}` on MDS Redis, not `ticker`.
- For Phase 1, subscribe to each configured supported order-book exchange; initially this is
  only `ticker:binance`.
- Store local receive time in `TickerPriceCache`.
- Add `TICKER_STALENESS_SEC` and return `None` for stale or non-positive prices.
- Preserve `ob_exec` priority:
  - fresh executable-side `ob_exec`
  - fresh ticker mid
  - no trigger price
- Reconnect the ticker subscriber after MDS Redis errors.

**Tests first:**

- Subscriber listens to `ticker:binance`.
- A publish to bare `ticker` does not update the cache.
- Stale ticker values are not returned.
- Fresh ticker remains the fallback when `ob_exec` is absent or stale.

**Verification:**

```bash
cd paper-trade-system/worker
python -m pytest tests/test_ticker_cache.py tests/test_ticker_subscriber.py tests/test_ob_exec_cache.py -v
```

**Acceptance:**

- Worker auto TP/SL never uses an indefinitely stale ticker.
- Ticker fallback consumes the channel MDS actually publishes.

**Commit:**

```bash
git commit -m "fix(paper-trade): consume fresh exchange-scoped MDS ticker fallback"
```

---

### Task 4: Make order-book subscription sync exchange-aware

**Files:**

- Modify: `paper-trade-system/worker/app/db.py`
- Modify: `paper-trade-system/worker/app/ob_subscribe.py`
- Modify: `paper-trade-system/worker/app/main.py`
- Modify: `paper-trade-system/worker/tests/test_db.py`
- Modify: `paper-trade-system/worker/tests/test_ob_subscribe.py`

**Implementation:**

- Replace the unscoped symbol query used by order-book sync with an exchange-aware query.
- Preferred DB API:

```python
async def get_open_symbols_by_exchange(self) -> dict[str, set[str]]:
    ...
```

- The sync loop publishes one `sync` message per supported exchange.
- Unsupported exchanges are excluded from order-book subscription.
- Event-driven subscribe after OPEN uses the persisted position/signal exchange and only
  publishes when that exchange is supported.
- On worker shutdown, publish an empty sync for every supported exchange before closing the
  MDS Redis client.

**Tests first:**

- Binance and OKX positions with the same symbol remain distinguishable.
- Binance sync contains only Binance symbols.
- OKX position does not publish to `orderbook:subscribe:binance`.
- Shutdown empty sync removes the worker's live depth demand.

**Verification:**

```bash
cd paper-trade-system/worker
python -m pytest tests/test_db.py tests/test_ob_subscribe.py tests/test_main.py -v
```

**Acceptance:**

- Phase-1 worker never requests Binance depth for a non-Binance position.

**Commit:**

```bash
git commit -m "fix(paper-trade): scope orderbook subscriptions by exchange"
```

---

### Task 5: Gate slippage RPC by supported exchange

**Files:**

- Modify: `paper-trade-system/worker/app/slippage_client.py`
- Modify: `paper-trade-system/worker/app/main.py`
- Modify: `paper-trade-system/worker/tests/test_slippage_client.py`
- Modify: `paper-trade-system/worker/tests/test_main_fill_wiring.py`

**Implementation:**

- Add supported-exchange gating at `FillService`, not only at call sites.
- For unsupported exchanges:
  - Do not enqueue an RPC request.
  - Immediately resolve through fixed-pct fallback.
- Keep the existing circuit breaker for supported exchanges when MDS is unavailable.
- Validate RPC responses before marking the circuit breaker successful:
  - JSON must decode to a dictionary.
  - Response `request_id`, when present, must match.
  - Numeric fill fields must be finite and non-negative.
- Treat malformed responses as failures and fall back.

**Tests first:**

- OKX fill returns fixed-pct without adding an `orderbook:slip:req:okx` item.
- Binance fill still sends RPC.
- Malformed JSON and invalid numeric responses count as failures.
- A valid response resets the breaker.

**Verification:**

```bash
cd paper-trade-system/worker
python -m pytest tests/test_slippage_client.py tests/test_fill.py tests/test_main_fill_wiring.py -v
```

**Acceptance:**

- Unsupported exchanges do not pay the RPC timeout.

**Commit:**

```bash
git commit -m "fix(paper-trade): gate L2 fills by supported exchange"
```

---

### Task 6: Preserve executable-price provenance through TP/SL and CLOSE

**Files:**

- Modify: `paper-trade-system/worker/app/ob_exec.py`
- Modify: `paper-trade-system/worker/app/executor.py`
- Modify: `paper-trade-system/worker/app/main.py`
- Modify: `paper-trade-system/worker/app/models.py`
- Modify: `paper-trade-system/alphas/base/engine.py`
- Modify alpha-specific close metadata builders only where they bypass the base helper
- Modify: `paper-trade-system/worker/tests/test_executor_tpsl_book.py`
- Modify: `paper-trade-system/worker/tests/test_main_fill_wiring.py`
- Modify: `paper-trade-system/alphas/base/tests/test_engine.py`

**Implementation:**

- Introduce an immutable price quote value carrying:
  - `price`
  - `source`
  - `is_executable`
- The worker TP/SL price provider returns:
  - `is_executable=True` for fresh `ob_exec` bid/ask.
  - `is_executable=False` for ticker mid.
- Preserve backward compatibility: executor still accepts the existing float/dict price
  sources in tests and legacy callers.
- Pass quote provenance directly to fill resolution. Do not re-query `ObExecCache` after a
  trigger, because the cache can become stale between trigger selection and fill fallback.
- Extend alpha close metadata produced from side-aware `price_alert` with
  `"ref_is_executable": true`.
- Candle/reversal/time exits remain non-executable unless explicitly marked.
- Parse close metadata once in the worker and pass `ref_is_executable` to `FillService`.
- Do not infer executability solely from `close_model` strings.

**Tests first:**

- Worker auto TP/SL triggered by `ob_exec` falls back to the exact bid/ask without an extra
  fixed-pct adjustment.
- If the cache becomes stale immediately after trigger selection, the selected quote still
  retains executable provenance.
- Alpha side-aware CLOSE fallback does not apply slippage twice.
- Candle CLOSE fallback still applies fixed-pct.
- Invalid metadata defaults to non-executable.

**Verification:**

```bash
cd paper-trade-system/worker
python -m pytest tests/test_executor_tpsl_book.py tests/test_main_fill_wiring.py tests/test_fill.py -v

cd ../alphas
PYTHONPATH=. python -m pytest base/tests/test_engine.py -v
```

**Acceptance:**

- No close path double-counts spread/fixed slippage when its reference is already executable.
- Non-executable references retain the legacy fallback model.

**Commit:**

```bash
git commit -m "fix(paper-trade): preserve executable-price provenance across closes"
```

---

### Task 7: Add end-to-end two-Redis integration tests

**Files:**

- Create: `paper-trade-system/worker/tests/test_mds_redis_integration.py`
- Optionally create shared fakes in: `paper-trade-system/worker/tests/helpers.py`

**Implementation:**

Build a focused integration harness with separate fake Redis instances:

- `paper_r`: contains `paper-signals`.
- `mds_r`: runs a minimal fake slippage responder and publishes `ob_exec`/ticker.

Cover these flows:

1. OPEN signal consumed from `paper_r` -> RPC sent to `mds_r` -> book average persisted.
2. OPEN success -> `orderbook:subscribe:binance` published on `mds_r`.
3. CLOSE from alpha executable bid/ask -> RPC timeout -> no double-slippage.
4. MDS unavailable -> paper signal still ACKed and fixed-pct fill persisted.
5. `ob_exec` stale -> ticker fallback from `mds_r`.
6. No `orderbook:*` keys are written to `paper_r`.

**Verification:**

```bash
cd paper-trade-system/worker
python -m pytest tests/test_mds_redis_integration.py -v
```

**Acceptance:**

- The test fails if paper and MDS Redis clients are accidentally swapped or merged.

**Commit:**

```bash
git commit -m "test(paper-trade): verify L2 flow across separate Redis instances"
```

---

### Task 8: Complete order-book observability

**Files:**

- Modify: `market-data-service/app/metrics.py`
- Modify: `market-data-service/app/orderbook/cache.py`
- Modify: `market-data-service/app/orderbook/slippage_rpc.py`
- Modify: `market-data-service/app/orderbook/book_tracker.py`
- Modify: `market-data-service/tests/test_orderbook_cache.py`
- Modify: `market-data-service/tests/test_slippage_rpc.py`
- Modify: `market-data-service/tests/test_book_tracker.py`
- Add worker structured counters/log summary in:
  `paper-trade-system/worker/app/slippage_client.py`
- Modify: `paper-trade-system/worker/tests/test_slippage_client.py`

**Implementation:**

Add the missing MDS metrics from the design:

- `mds_slippage_rpc_latency_ms` gauge or histogram-compatible summary fields.
- `mds_slippage_rpc_book_too_shallow_total`.
- `mds_depth_rest_cache_hits_total`.
- `mds_orderbook_book_state{exchange,state}` as aggregate counts, avoiding an unbounded
  per-symbol label.

Add worker-side structured counters or periodic summary logs for:

- RPC success by source.
- RPC timeout/error.
- Circuit-breaker short-circuit.
- Fixed-pct fallback.
- Executable-reference fallback.

Do not add high-cardinality request IDs or symbols as metric labels.

**Tests first:**

- REST cache hit increments once.
- Shallow response increments once.
- RPC latency is recorded for success and fallback.
- Breaker short-circuit is distinguishable from timeout.

**Verification:**

```bash
cd market-data-service
python -m pytest tests/test_orderbook_cache.py tests/test_slippage_rpc.py tests/test_book_tracker.py -v

cd ../paper-trade-system/worker
python -m pytest tests/test_slippage_client.py -v
```

**Acceptance:**

- Operators can prove whether fills came from live book, REST, or fixed fallback.

**Commit:**

```bash
git commit -m "feat(orderbook): expose L2 usage and fallback observability"
```

---

### Task 9: Replace stale-file healthchecks and harden Redis services

**Files:**

- Modify: `market-data-service/docker-compose.yml`
- Modify: `paper-trade-system/docker-compose.yml`
- Create: `paper-trade-system/worker/app/healthcheck.py`
- Modify: `paper-trade-system/worker/app/main.py`
- Create: `paper-trade-system/worker/tests/test_healthcheck.py`
- Modify MDS health tests or create: `market-data-service/tests/test_http_server.py`

**Implementation:**

- Add `restart: unless-stopped` and Redis `PING` healthchecks to both Redis services.
- Use `depends_on: condition: service_healthy` for services defined in the same compose
  project. The worker cannot `depends_on` the external MDS Redis service, so its MDS path
  must remain reconnecting/degradable.
- MDS service healthcheck calls its existing `http://127.0.0.1:9090/health` endpoint via
  Python stdlib so no curl/wget dependency is required.
- MDS `/health` must not remain `"ok"` with exchange status `"starting"` forever. Allow a
  bounded startup grace period, then return degraded/503 if no ticker or kline has arrived.
- Worker healthcheck validates:
  - heartbeat file exists and is recent;
  - paper Redis is reachable;
  - process has advanced beyond startup.
- MDS Redis is reported as a degraded dependency for worker L2, but does not make the worker
  unhealthy because fixed-pct fallback is intentional.
- Remove any stale heartbeat at worker startup before connecting to Redis.
- Write heartbeat atomically with current timestamp, not static `"ok"`.
- Document that code 137 requires host-memory investigation; restart policy restores service
  but does not solve host OOM.

**Tests first:**

- Old heartbeat fails healthcheck.
- Fresh heartbeat plus paper Redis succeeds.
- Fresh heartbeat plus unavailable paper Redis fails.
- MDS `/health` returns 503 when exchange data is stale.

**Verification:**

```bash
cd paper-trade-system/worker
python -m pytest tests/test_healthcheck.py -v

cd ../../market-data-service
python -m pytest tests/test_http_server.py -v

docker compose -f docker-compose.yml config
docker compose -f ../paper-trade-system/docker-compose.yml config
```

**Acceptance:**

- A container stuck retrying Redis cannot remain falsely healthy due to an old file.
- Redis containers restart after unexpected termination.

**Commit:**

```bash
git commit -m "fix(runtime): make Redis and service healthchecks truthful"
```

---

### Task 10: Fix test isolation and run full regression suites

**Files:**

- Modify: `paper-trade-system/worker/tests/test_config.py`
- Modify any tests that instantiate settings without `_env_file=None`
- Modify test fixtures only as needed

**Implementation:**

- Ensure tests for defaults explicitly disable `.env` loading.
- Add a separate test proving `.env`/environment override behavior.
- Do not modify production defaults merely to satisfy local `.env`-dependent tests.

**Verification:**

```bash
cd paper-trade-system/worker
python -m pytest tests -q

cd ../../market-data-service
python -m pytest tests -q

cd ../paper-trade-system/alphas
PYTHONPATH=. python -m pytest base/tests -q
```

**Acceptance:**

- All three suites pass from a checkout containing the current local `.env` files.

**Commit:**

```bash
git commit -m "test: isolate config defaults from local env files"
```

---

### Task 11: Docker end-to-end smoke test and rollout

**Files:**

- Create: `paper-trade-system/scripts/test_orderbook_e2e.py`
- Modify: `paper-trade-system/README.md`
- Modify: `market-data-service/docs/alpha-mds-integration-guide.md`

**Smoke-test behavior:**

The script must:

1. Connect independently to paper Redis and MDS Redis.
2. Publish an `orderbook:subscribe:binance` sync on MDS Redis.
3. Wait for a fresh `ob_exec:binance:BTCUSDT`.
4. Submit a small BUY and SELL slippage RPC and validate:
   - matching request ID;
   - `source` is `live_book` or `rest`;
   - `fallback_used=false`;
   - positive average price;
   - requested and filled quantity are valid.
5. Confirm no request appeared on paper Redis.
6. Print latency and source without placing a paper-trade order.

**Rollout order:**

1. Stop worker and alphas.
2. Start/restart MDS Redis and MDS.
3. Confirm MDS `/health` and `/metrics`.
4. Run the smoke test directly against MDS.
5. Start paper Redis and worker.
6. Confirm worker connects to both Redis instances.
7. Start one alpha only.
8. Observe one OPEN and one CLOSE:
   - MDS RPC counter increments.
   - `orderbook_subscribed_symbols` increments after OPEN.
   - trade metadata records fill price and source/provenance.
9. Start remaining alphas.

**Verification commands:**

```bash
docker compose -f market-data-service/docker-compose.yml up -d --build
docker compose -f paper-trade-system/docker-compose.yml up -d --build

python paper-trade-system/scripts/test_orderbook_e2e.py

docker compose -f market-data-service/docker-compose.yml ps
docker compose -f paper-trade-system/docker-compose.yml ps
```

**Runtime acceptance checklist:**

- [ ] Both Redis containers are running and healthy.
- [ ] Worker resolves `paper-redis` and `mds-redis`.
- [ ] MDS logs `Order-book subsystem started`.
- [ ] Worker does not log repeated slippage RPC timeouts during normal operation.
- [ ] MDS `slippage_rpc_requests_total{source="live_book"}` or `{source="rest"}` increases.
- [ ] MDS `orderbook_subscribed_symbols{exchange="binance"}` reflects open positions.
- [ ] Worker uses fixed-pct only during an intentional MDS outage or unsupported exchange.
- [ ] Stopping MDS causes graceful fallback without stopping paper signal consumption.
- [ ] Restarting MDS restores L2 use without restarting the worker.

**Commit:**

```bash
git commit -m "test(runtime): add orderbook L2 end-to-end smoke verification"
```

---

## Final Acceptance Criteria

The fix is complete only when all of the following are true:

1. Paper signal traffic and MDS order-book traffic use explicitly separate Redis clients.
2. A Binance OPEN/CLOSE can receive a non-fallback book-walked fill in Docker runtime.
3. Unsupported exchanges fall back immediately without RPC timeout.
4. Worker auto TP/SL uses fresh executable-side book price, then fresh ticker mid.
5. Alpha-managed side-aware CLOSE does not apply fixed slippage twice.
6. No stale `ob_exec`, stale ticker, or stale health file is treated as current.
7. Metrics demonstrate live-book/REST/fallback usage and failure reasons.
8. Redis/service health reflects actual availability.
9. Worker, MDS, and alpha-base full test suites pass.

## Recommended Execution Order

Execute Tasks 1-7 first as the correctness-critical path. Task 8 adds proof and operational
visibility. Tasks 9-11 make the deployment reliable and verify that the corrected code is
actually active.
