# Paper Trade Live-Accuracy and Position-Recovery Hardening Plan

> **For agentic workers:** Execute task-by-task. Write focused tests before production
> changes. Do not mount the worker SQLite database into alpha containers. Keep the worker
> database as the only authoritative position ledger.

**Goal:** Close the remaining accuracy and lifecycle gaps between paper trading and live
market-order execution:

1. Restore and continuously reconcile alpha-managed positions after alpha restarts.
2. Make healthchecks fail when authoritative positions are not actually managed or
   subscribed to realtime price alerts.
3. Pre-subscribe orderbook depth before resolving an OPEN fill.
4. Model execution latency and adverse price movement after the initial book walk.

**Depends on:**

- `paper-trade-system/docs/superpowers/plans/2026-06-09-orderbook-l2-papertrade.md`
- `paper-trade-system/docs/superpowers/plans/2026-06-10-orderbook-l2-runtime-correctness-fix.md`
- `market-data-service/docs/superpowers/plans/2026-06-09-orderbook-l2-mds.md`

**Observed baseline on 2026-06-10:**

- Worker SQLite held seven open positions after alpha containers restarted.
- The running alpha containers had empty in-memory `_open_positions`.
- MDS reported:
  - `mds_price_alert_active_consumers{exchange="binance"} 0`
  - `mds_price_alert_subscribed_symbols{exchange="binance"} 0`
- Worker auto TP/SL was disabled, so those positions had no realtime exit owner.
- Alpha signal to worker receipt: median approximately `1.07ms`, p95 approximately `8.64ms`.
- Live-book slippage RPC end-to-end: median approximately `5.07ms`, p95 approximately
  `8.09ms`.
- Active-position live snapshot age at response: median approximately `134ms`, p95
  approximately `181ms`.
- An OPEN-like fill for a symbol without a live depth subscription used REST depth in
  approximately `125-211ms`.

**Accuracy principle:** The system must distinguish:

- **Decision price:** the price that caused an alpha to open or close.
- **Trigger price:** executable-side bid/ask that crossed a TP/SL condition.
- **Initial book-walk price:** fill calculated when the worker receives the signal.
- **Delayed execution price:** fill calculated after simulated order-transmission latency.
- **Recorded fill price:** the adverse result selected from the execution model.

Do not silently overwrite or collapse these prices into one field.

---

## Architecture Decisions

### Authoritative position ownership

- Worker SQLite remains the source of truth for open positions.
- Alphas must not read or mount SQLite directly.
- Worker mirrors authoritative per-alpha snapshots into paper Redis:

```text
paper:positions:snapshot:{alpha_id}
```

- Snapshot value is one atomic JSON object:

```json
{
  "alpha_id": "alpha-1-v5b",
  "revision": 42,
  "generated_at": "2026-06-10T04:00:00Z",
  "positions": []
}
```

- Worker republishes snapshots:
  - at worker startup;
  - after every committed OPEN, MODIFY, partial CLOSE, and full CLOSE;
  - periodically as a repair mechanism.
- Alpha reconciles local state from this snapshot before becoming `LIVE`, then continues
  reconciling periodically.

### Alpha runtime ownership heartbeat

Each alpha publishes a TTL-backed runtime ownership record to paper Redis:

```text
paper:alpha-runtime:{alpha_id}
```

Payload:

```json
{
  "alpha_id": "alpha-1-v5b",
  "authoritative_revision": 42,
  "managed_position_ids": ["..."],
  "managed_symbols": ["BTCUSDT"],
  "desired_price_alert_symbols": ["BTCUSDT"],
  "runtime_state": "LIVE",
  "reconciled_at": "2026-06-10T04:00:00Z"
}
```

The record expires unless the alpha refreshes it. A container being alive is not proof that
it manages its authoritative positions.

### Recovery conflict rules

- Position present in worker snapshot but missing locally: restore locally.
- Position present locally but absent from worker snapshot: remove locally without emitting
  a CLOSE signal.
- Same `position_id` with different qty, entry, TP, or SL: worker snapshot wins.
- A recovered alpha must never emit a duplicate OPEN.
- A recovered alpha must not widen an authoritative stop.
- Existing legacy positions with incomplete strategy state enter `RECOVERED_CONSERVATIVE`
  mode:
  - preserve worker TP/SL;
  - subscribe realtime price alerts;
  - allow CLOSE and stop-tightening;
  - do not infer a looser trailing stop.

### Pre-OPEN depth policy

Before resolving an OPEN fill:

1. Publish an event-driven orderbook subscribe for the symbol.
2. Wait a bounded period for a fresh `READY` `ob_exec`.
3. Resolve the fill through the normal slippage RPC.
4. If live depth is not ready before the deadline, continue with REST depth.
5. Never wait indefinitely and never reject an otherwise valid OPEN solely because live
   depth did not become ready.

### Execution-latency model

Do not add an arbitrary favorable or adverse bps adjustment directly to the first fill.
Use a delayed re-quote model:

1. Walk the current book.
2. Wait configured simulated order-transmission latency.
3. Walk the latest book again.
4. Select the adverse price:
   - BUY: `max(initial_avg, delayed_avg)`
   - SELL: `min(initial_avg, delayed_avg)`
5. Optionally apply a configured minimum adverse floor after the delayed re-quote.

The latency model must be deterministic in unit tests and disabled by default until its
metrics are reviewed.

---

## Non-Goals

- Replacing worker SQLite with Redis.
- Letting alpha state override authoritative worker position fields.
- Implementing real exchange order acknowledgements.
- Simulating queue position for limit orders.
- Implementing orderbook support beyond exchanges already supported by MDS.
- Guaranteeing exact live fills when liquidity disappears faster than market data arrives.

---

## Phase 1: Authoritative Position Snapshot Contract

### Task 1: Add worker position snapshot publisher

**Files:**

- Create: `worker/app/position_snapshots.py`
- Modify: `worker/app/db.py`
- Modify: `worker/app/main.py`
- Modify: `worker/app/executor.py`
- Create: `worker/tests/test_position_snapshots.py`
- Modify: `worker/tests/test_main.py`

**Implementation:**

- Add a `PositionSnapshotPublisher` using paper Redis only.
- Serialize public position fields plus parsed metadata.
- Allocate revisions with a Redis `INCR` key; publish each per-alpha snapshot as one JSON
  value so readers never see a partially updated set.
- Publish empty snapshots for alphas whose last position closed.
- Republish all registered-alpha snapshots on worker startup.
- Add a periodic repair loop, default `POSITION_SNAPSHOT_SYNC_INTERVAL_SEC=5`.
- Publish only after the SQLite transaction commits.
- A snapshot publish failure must not roll back a successfully processed signal; log it and
  let the repair loop heal the mirror.

**Tests first:**

- Snapshot contains only positions belonging to the requested alpha.
- OPEN produces a snapshot containing the committed position.
- MODIFY publishes updated TP/SL.
- Partial CLOSE publishes reduced qty.
- Full CLOSE publishes an empty snapshot.
- Startup republishes DB positions even when Redis contains stale data.
- Snapshot failure does not cause duplicate signal processing.

**Acceptance:**

- An alpha can reconstruct all authoritative open-position fields without SQLite access.
- Redis snapshots converge to SQLite within the configured repair interval.

**Commit:**

```bash
git commit -m "feat(paper-trade): publish authoritative position snapshots"
```

---

### Task 2: Define strategy-state persistence and recovery hooks

**Files:**

- Modify: `alphas/base/engine.py`
- Modify: `alphas/base/config.py`
- Modify: `alphas/base/tests/test_engine.py`
- Modify: `worker/app/db.py`
- Modify: `worker/app/executor.py`
- Modify: `worker/app/models.py`
- Modify: `worker/tests/test_db.py`
- Modify: `worker/tests/test_executor.py`

**Implementation:**

- Add BaseEngine hooks:
  - `restore_position(snapshot_position) -> dict | None`
  - `serialize_position_runtime(position) -> dict`
  - `on_position_reconciled(position, mode)`.
- Extend MODIFY handling so metadata can merge a namespaced
  `strategy_runtime` object into position metadata without replacing immutable entry
  metadata.
- Require alphas to persist state that cannot be reconstructed from worker fields:
  - trailing extrema;
  - trail distance;
  - entry candle timing/min-hold state;
  - partial-TP counters;
  - break-even activation;
  - strategy-specific remaining state.
- Keep worker-owned fields outside `strategy_runtime`.
- Add metadata versioning:

```json
{
  "strategy_runtime_version": 1,
  "strategy_runtime": {}
}
```

- Legacy metadata without runtime state must restore in `RECOVERED_CONSERVATIVE` mode.

**Tests first:**

- MODIFY merges runtime metadata while preserving entry metadata.
- Malformed runtime metadata cannot corrupt authoritative fields.
- Recovery hooks receive normalized worker fields.
- Legacy position recovery is conservative and never widens SL.

**Acceptance:**

- Every strategy has an explicit answer for which state is authoritative, reconstructible,
  or persisted.

**Commit:**

```bash
git commit -m "feat(alpha-base): define durable position recovery contract"
```

---

## Phase 2: Alpha Reconciliation and Realtime Ownership

### Task 3: Add BaseEngine reconciliation loop

**Files:**

- Create: `alphas/base/position_reconcile.py`
- Modify: `alphas/base/engine.py`
- Modify: `alphas/base/config.py`
- Modify: `alphas/base/tests/test_engine.py`
- Create: `alphas/base/tests/test_position_reconcile.py`

**Implementation:**

- Add configuration:
  - `POSITION_RECONCILE_INTERVAL_SEC=5`
  - `POSITION_SNAPSHOT_MAX_AGE_SEC=15`
  - `ALPHA_RUNTIME_HEARTBEAT_TTL_SEC=20`
  - `POSITION_RECONCILE_STARTUP_TIMEOUT_SEC=30`.
- Before setting runtime state to `LIVE`, load the authoritative snapshot and reconcile.
- Periodically reconcile while running.
- After reconciliation, call `mark_positions_changed()` so price-alert Pub/Sub and MDS
  subscription sync refresh immediately.
- Publish the alpha runtime ownership heartbeat after each successful reconcile and on a
  short periodic interval.
- Mark alpha `STALE` when:
  - authoritative snapshot is older than the allowed age;
  - snapshot cannot be read beyond the grace period;
  - a worker-owned position cannot be safely restored.
- Do not emit OPEN/CLOSE signals during reconciliation itself.

**Tests first:**

- Startup restores missing local positions before `LIVE`.
- Local ghost positions are removed without CLOSE.
- Revision changes trigger reconciliation.
- Identical revision is idempotent.
- Missing/stale authoritative snapshot prevents `LIVE`.
- Heartbeat contains exact managed IDs and desired price-alert symbols.

**Acceptance:**

- Restarting an alpha does not orphan worker positions or duplicate them.

**Commit:**

```bash
git commit -m "feat(alpha-base): reconcile positions from worker authority"
```

---

### Task 4: Implement strategy-specific restoration

**Files:**

- Modify and test:
  - `alphas/alpha-1-v5b/app/engine.py`
  - `alphas/alpha-1-v5b-5pct/app/engine.py`
  - `alphas/alpha-1-v5b-reverse/app/engine.py`
  - `alphas/alpha-1-q1/app/engine.py`
  - `alphas/alpha-1-q2/app/engine.py`
  - `alphas/alpha-1-q3/app/engine.py`
  - `alphas/hyper-turbo/app/engine.py`
- Add focused recovery tests in each alpha's test directory.

**Implementation:**

- V5/Q family:
  - restore position ID, side, actual worker entry fill, qty, TP, SL, trade size,
    trail distance, min-hold timing, `hse`, and `lse`;
  - derive conservative extrema from authoritative SL plus trail distance when legacy
    runtime metadata is incomplete;
  - persist new extrema whenever trailing state changes.
- Hyper Turbo:
  - persist and restore initial qty, remaining qty, TP hit count, break-even state, and last
    partial-TP signal bar;
  - derive remaining qty from worker authority;
  - legacy ambiguous positions must not repeat an already-executed partial TP.
- Update all strategies to use the worker's actual entry fill after reconciliation rather
  than the alpha's original decision price.
- Ensure all restored active symbols become price-alert subscriptions, even when a
  strategy currently only uses alerts in a later state such as break-even.

**Tests first:**

- Restart fixture: create local position, serialize state, construct fresh engine, reconcile,
  and verify equivalent management behavior.
- Recovered positions trigger TP/SL on side-aware price alerts.
- Recovered partial position does not repeat prior partial CLOSE.
- Legacy recovery never widens SL or emits duplicate OPEN.

**Acceptance:**

- Every deployed alpha can safely own its worker positions after restart.

**Commit:**

```bash
git commit -m "fix(alphas): restore managed positions after restart"
```

---

## Phase 3: Health and Ownership Enforcement

### Task 5: Make alpha health reflect reconciliation

**Files:**

- Modify: `alphas/base/engine.py`
- Modify: `alphas/base/tests/test_engine.py`
- Modify: all active alpha `docker-compose.yml` healthchecks.

**Implementation:**

- Replace unconditional `/tmp/bot_health` writes with a timestamped health record.
- Alpha is healthy only when:
  - authoritative snapshot is fresh;
  - local managed IDs exactly match authoritative IDs;
  - runtime heartbeat was published successfully;
  - each active symbol is included in the alpha's desired price-alert set.
- Remove stale health file at process startup.
- Docker healthcheck must reject an old health timestamp.

**Tests first:**

- Alpha with zero authoritative and zero local positions is healthy.
- Missing local authoritative position is unhealthy.
- Local ghost position is unhealthy until reconciliation removes it.
- Stale snapshot and stale heartbeat are unhealthy.

**Acceptance:**

- A restarted alpha with orphaned positions cannot report healthy.

**Commit:**

```bash
git commit -m "fix(alpha-base): fail health on position ownership mismatch"
```

---

### Task 6: Add worker ownership and subscription monitor

**Files:**

- Create: `worker/app/position_ownership.py`
- Modify: `worker/app/main.py`
- Modify: `worker/app/config.py`
- Modify: `worker/tests/test_main.py`
- Create: `worker/tests/test_position_ownership.py`
- Modify: `docker-compose.yml`

**Implementation:**

- Compare worker DB positions against:
  - `paper:alpha-runtime:{alpha_id}` in paper Redis;
  - actual MDS Redis subscription set
    `price_alert:subscriptions:{exchange}:{alpha_id}`.
- Add grace settings:
  - `POSITION_OWNERSHIP_GRACE_SEC=30`
  - `POSITION_OWNERSHIP_CHECK_INTERVAL_SEC=5`.
- Worker health becomes unhealthy when a mismatch persists beyond grace.
- Log structured mismatch details: alpha, missing IDs, extra IDs, and missing subscriptions.
- Do not stop signal consumption solely because ownership health is degraded.
- Expose mismatch count in worker logs and health payload.

**Tests first:**

- Exact alpha heartbeat and MDS subscriptions are healthy.
- Missing alpha heartbeat is unhealthy after grace.
- Missing price-alert subscription is unhealthy after grace.
- Temporary mismatch inside grace does not flap health.
- Paper Redis outage and MDS Redis outage are reported separately.

**Acceptance:**

- Worker cannot remain healthy while DB positions have no realtime manager.

**Commit:**

```bash
git commit -m "fix(paper-trade): monitor position ownership and price-alert coverage"
```

---

## Phase 4: Pre-Subscribe Before OPEN

### Task 7: Add exchange-scoped readiness waiting to `ObExecCache`

**Files:**

- Modify: `worker/app/ob_exec.py`
- Modify: `worker/tests/test_ob_exec_cache.py`
- Modify: `worker/app/config.py`
- Modify: `worker/tests/test_config.py`

**Implementation:**

- Key cache entries by `(exchange, symbol)`, not symbol alone.
- Add an async readiness waiter notified when a fresh `READY` quote arrives.
- Add configuration:
  - `OPEN_BOOK_PRE_SUBSCRIBE_ENABLED=true`
  - `OPEN_BOOK_READY_TIMEOUT_MS=750`
  - `OPEN_BOOK_MAX_AGE_MS=500`.
- Waiting must return immediately if a fresh READY book already exists.
- Cancellation and timeout must remove waiter state.

**Tests first:**

- Exchange-scoped symbols do not collide.
- Existing READY quote returns immediately.
- Waiter wakes on fresh READY update.
- STALE/non-ready update does not wake as successful.
- Timeout and cancellation leave no waiter leak.

**Acceptance:**

- Worker can deterministically know whether live depth became ready before OPEN fill
  resolution.

**Commit:**

```bash
git commit -m "feat(paper-trade): wait for exchange-scoped live book readiness"
```

---

### Task 8: Pre-subscribe before resolving OPEN fills

**Files:**

- Modify: `worker/app/main.py`
- Modify: `worker/app/ob_subscribe.py`
- Modify: `worker/tests/test_main_fill_wiring.py`
- Modify: `worker/tests/test_ob_subscribe.py`

**Implementation:**

- For supported exchanges, before `FillService.resolve()` for OPEN:
  - publish orderbook subscribe;
  - wait up to `OPEN_BOOK_READY_TIMEOUT_MS`;
  - then resolve the fill.
- Record pre-subscribe outcome in execution metadata:
  - already ready;
  - became ready;
  - timed out;
  - publish failed;
  - unsupported exchange.
- Preserve REST/fixed fallback on timeout or failure.
- Keep the periodic authoritative sync loop; it removes temporary subscriptions for
  rejected/failed OPENs.
- Do not perform pre-subscribe for duplicate or malformed OPENs that can be rejected before
  fill resolution.

**Tests first:**

- OPEN publishes subscribe before slippage RPC.
- READY arrival causes RPC to use live book.
- Timeout still processes OPEN through REST/fallback.
- Unsupported exchange skips pre-subscribe.
- Rejected duplicate OPEN does not retain a permanent subscription.

**Acceptance:**

- Normal Binance OPENs prefer live depth without risking an unbounded entry delay.

**Commit:**

```bash
git commit -m "feat(paper-trade): pre-subscribe live depth before open fills"
```

---

## Phase 5: Execution Latency and Adverse Movement

### Task 9: Introduce structured fill resolutions

**Files:**

- Modify: `worker/app/slippage_client.py`
- Modify: `worker/app/fill.py`
- Modify: `worker/app/executor.py`
- Modify: `worker/app/main.py`
- Modify: `worker/tests/test_fill.py`
- Modify: `worker/tests/test_slippage_client.py`
- Modify: `worker/tests/test_executor_fill_price.py`

**Implementation:**

- Replace the internal float-only fill result with a `FillResolution` dataclass containing:
  - final price;
  - order side;
  - initial and delayed prices;
  - initial and delayed sources/book states;
  - snapshot timestamps and ages;
  - requested/filled qty;
  - model latency;
  - adverse movement bps;
  - fallback reason.
- Preserve backward compatibility: executor still accepts a raw float from legacy callers.
- Merge execution metadata into position metadata on OPEN and trade metadata on CLOSE.
- Never label REST or fixed-pct fills as live-book fills.

**Tests first:**

- Structured resolution serializes into metadata.
- Legacy float path remains unchanged.
- OPEN and CLOSE metadata preserve decision, trigger, and execution prices separately.

**Acceptance:**

- Every recorded fill can be audited back to its market-data source and model decisions.

**Commit:**

```bash
git commit -m "feat(paper-trade): record structured execution resolutions"
```

---

### Task 10: Add delayed adverse re-quote execution model

**Files:**

- Create: `worker/app/execution_model.py`
- Modify: `worker/app/slippage_client.py`
- Modify: `worker/app/config.py`
- Modify: `worker/.env`
- Modify: `docker-compose.yml`
- Create: `worker/tests/test_execution_model.py`
- Modify: `worker/tests/test_slippage_client.py`

**Implementation:**

- Add configuration:
  - `EXECUTION_LATENCY_MODEL_ENABLED=false`
  - `EXECUTION_LATENCY_MS=50`
  - `EXECUTION_MIN_ADVERSE_BPS=0`
  - `EXECUTION_SECOND_QUOTE_TIMEOUT_MS=200`.
- Implement delayed re-quote:
  - resolve initial book walk;
  - sleep using injected async sleeper;
  - resolve the same order side and qty again;
  - select the adverse result by order side.
- If delayed quote fails:
  - retain the initial valid book walk;
  - mark delayed quote failure in metadata;
  - optionally apply configured minimum adverse floor.
- Do not double-apply fixed slippage.
- Keep the model disabled by default for the first deployment.

**Tests first:**

- BUY selects the higher delayed price.
- SELL selects the lower delayed price.
- Favorable delayed movement does not improve the recorded fill.
- Delayed RPC failure retains initial valid fill.
- Injected sleeper makes tests deterministic.
- Disabled model performs one RPC and preserves current behavior.

**Acceptance:**

- Enabling the model produces conservative, auditable fills without arbitrary favorable
  movement.

**Commit:**

```bash
git commit -m "feat(paper-trade): model delayed adverse execution"
```

---

## Phase 6: Observability, Migration, and End-to-End Verification

### Task 11: Add lifecycle and execution observability

**Files:**

- Modify: `worker/app/main.py`
- Modify: `worker/app/position_ownership.py`
- Modify: `worker/app/execution_model.py`
- Modify: `market-data-service/app/metrics.py` only if additional MDS metrics are required.
- Add focused tests for emitted health and metric payloads.

**Metrics/logs:**

- Position reconciliation:
  - authoritative count;
  - locally managed count;
  - restored/dropped count;
  - revision and snapshot age.
- Ownership health:
  - orphaned position count;
  - missing price-alert subscription count;
  - heartbeat age.
- OPEN pre-subscribe:
  - ready success;
  - timeout;
  - publish failure;
  - wait latency.
- Execution model:
  - initial source;
  - delayed source;
  - model latency;
  - adverse movement bps;
  - second-quote failure.

**Acceptance:**

- Operators can prove whether a position is owned, subscribed, and filled through live book,
  REST, or fallback without inspecting source code.

**Commit:**

```bash
git commit -m "feat(paper-trade): expose position and execution accuracy health"
```

---

### Task 12: Migrate existing positions and run restart/chaos verification

**Files:**

- Create: `scripts/audit_position_ownership.py`
- Create: `scripts/smoke_test_live_accuracy.py`
- Update: operator documentation and this plan with measured results.

**Migration:**

- Back up worker SQLite before rollout.
- Audit every existing position for sufficient metadata.
- Mark legacy positions requiring conservative recovery.
- Deploy worker snapshot publisher first.
- Deploy alpha reconciliation second.
- Confirm alpha ownership and price-alert subscriptions before enabling strict health.
- Enable pre-subscribe after ownership is healthy.
- Keep latency model disabled until baseline metrics are collected.

**Required end-to-end scenarios:**

1. Restart one alpha while its position remains open:
   - position is restored;
   - no duplicate OPEN;
   - price-alert subscription returns;
   - health returns only after reconciliation.
2. Restart worker:
   - snapshots republish from SQLite;
   - alphas converge to the new revision.
3. Restart MDS Redis/MDS:
   - position ownership remains known;
   - subscriptions recover;
   - fills degrade visibly to fallback.
4. OPEN unsubscribed symbol:
   - pre-subscribe attempted;
   - live book preferred when ready;
   - bounded REST fallback otherwise.
5. CLOSE during volatile movement:
   - delayed adverse re-quote never improves the fill.
6. Partial CLOSE followed by alpha restart:
   - remaining qty and strategy state restore;
   - no repeated partial leg.

**Verification commands:**

```bash
cd paper-trade-system/worker
python -m pytest -q

cd paper-trade-system/alphas
PYTHONPATH=. python -m pytest base/tests -q

cd market-data-service
python -m pytest -q

cd paper-trade-system
python scripts/audit_position_ownership.py
python scripts/smoke_test_live_accuracy.py
docker compose ps -a
```

**Final acceptance:**

- Every authoritative worker position has exactly one healthy alpha owner.
- Every active Binance position has an actual MDS price-alert subscription.
- Alpha restart does not orphan, duplicate, or accidentally close positions.
- Supported OPENs prefer live orderbook within a bounded wait.
- Fill metadata distinguishes decision, trigger, initial walk, delayed walk, and final fill.
- The execution-latency model is conservative, measurable, and independently switchable.

**Commit:**

```bash
git commit -m "test(paper-trade): verify restart-safe live-accuracy lifecycle"
```

---

## Recommended Rollout Order

1. Tasks 1-4: authoritative snapshots and strategy restoration.
2. Tasks 5-6: health enforcement after reconciliation is proven.
3. Tasks 7-8: bounded pre-OPEN subscription.
4. Tasks 9-11: structured fills, delayed execution model, and observability.
5. Task 12: migrate existing positions and run chaos verification.

Do not enable strict health or execution-latency modeling before position reconciliation is
deployed and existing positions have been audited.
