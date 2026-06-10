# Order-Book Slippage Logging for All Alphas — Design

Date: 2026-06-10
Status: Approved (pending written-spec review)
Scope: `paper-trade-system/worker`

## 1. Problem & Goal

Every alpha's order execution flows through the worker's central fill path
(`worker/app/main.py` → `FillService.resolve()` → `Executor.process_open` /
`process_close` / `check_tpsl_hits`). The order-book slippage RPC already returns a
`slippage_bps` (book-walk impact: `avg_exec_price` vs best price), but
`FillService.resolve()` **discards** it, so it never appears in the logs.

**Goal:** for every alpha, log the order-book-computed slippage right at each entry and
exit, on the same log line as the existing trade event. Because execution is centralized,
logging in the worker fill path covers all alphas automatically.

Decisions locked during brainstorming:
- **Metric:** the RPC's `slippage_bps` (book-walk impact), with `source`
  (live_book / rest / fixed_pct) and `book_state` for context. NOT slippage-vs-signal-price.
- **Destination:** application log only (`logger.info`). No DB column, no migration.
- **Coverage:** entry (OPEN) + all exits — signal CLOSE *and* SL/TP auto-exit.
- **Style:** extend the existing entry/exit log lines ("log bên cạnh"), not a separate
  log stream.

## 2. Non-Goals

- Persisting slippage to the database / dashboard surfacing (app log only).
- A new dedicated `[SLIPPAGE]` log line per event (rejected: doubles log volume, splits
  related info).
- Changing how slippage is computed (the RPC already computes `slippage_bps`; we only
  surface it).
- Slippage-vs-signal-price (the gap between the alpha's intended price and the fill).

## 3. Current Flow (context)

- `worker/app/main.py::process_signal_message` resolves the fill OUTSIDE the DB
  transaction via `FillService.resolve(...)` for OPEN and signal-CLOSE, then calls
  `executor.process_open(signal, fill_price=...)` / `process_close(...)`.
- SL/TP auto-exit: `run_price_check_loop` → `executor.check_tpsl_hits(price_fn, fill_resolver=...)`,
  where `fill_resolver` calls `FillService.resolve(...)`.
- `Executor._fill_value_and_metadata(fill_price)` turns a `FillResolution` into
  `(final_price, {"execution": fill_price.metadata()})`, where `metadata()` is
  `asdict(self)`. This `execution_metadata` is already built at all three points
  (`process_open`, `process_close`, `check_tpsl_hits`).
- Existing log lines: `[CLOSE]` (`executor.py:157`) and `[TP_HIT]/[SL_HIT]`
  (`executor.py:276`). **`process_open` has no log line today** — one is added.

## 4. Design

### 4.1 Surface `slippage_bps` into `FillResolution`

`worker/app/slippage_client.py`:
- Add field `book_slippage_bps: float | None = None` to the `FillResolution` dataclass
  (named distinctly from the existing `adverse_movement_bps` latency-model field).
- In `FillService.resolve()`, after the `resolution = FillResolution(...)` is built, set:
  ```python
  if resp is not None and not resp.get("fallback_used"):
      try:
          resolution.book_slippage_bps = float(resp["slippage_bps"])
      except (KeyError, TypeError, ValueError):
          resolution.book_slippage_bps = None
  ```
  It stays `None` for fixed_pct / rpc_unavailable / unsupported_exchange / malformed
  responses, so the log can honestly show "no orderbook slippage" in those cases.
- Because `metadata()` is `asdict(self)`, the new field automatically flows into
  `execution_metadata["execution"]` at every log point — no further plumbing.

The book-walk slippage logged is the **initial** resolution's value (the entry/exit
book-walk). The latency-model delayed quote does not change this number.

### 4.2 Formatter

`worker/app/fill.py` (next to the other pure fill helpers):
```python
def book_slippage_suffix(execution_metadata: dict) -> str:
    """Render the order-book slippage as a log suffix from an executor's
    execution_metadata ({"execution": <FillResolution asdict>} or {})."""
    execution = (execution_metadata or {}).get("execution")
    if not execution:
        return ""
    bps = execution.get("book_slippage_bps")
    source = execution.get("initial_source", "unknown")
    if bps is None:
        reason = execution.get("fallback_reason")
        tail = f"{source}({reason})" if reason else source
        return f" | book_slip=n/a src={tail}"
    state = execution.get("initial_book_state")
    state_part = f" state={state}" if state else ""
    return f" | book_slip={bps:.1f}bps src={source}{state_part}"
```
- Book-sourced → `" | book_slip=8.4bps src=live_book state=READY"`.
- Fallback → `" | book_slip=n/a src=fixed_pct(rpc_unavailable)"`.
- No execution metadata (plain float fill) → `""` (clean line, no dangling suffix).

### 4.3 Wire into the three log points

All read the `execution_metadata` already present at each point.

- **ENTRY — `Executor.process_open` (executor.py, before `return`):** add
  ```python
  logger.info(
      "[OPEN] %s %s %s qty=%.8f fill=%.6f%s",
      signal.alpha_id, signal.side, signal.symbol, signal.qty, fill_price,
      book_slippage_suffix(execution_metadata),
  )
  ```
- **EXIT (signal CLOSE) — executor.py:157:** append `%s` + `book_slippage_suffix(execution_metadata)`
  to the existing `[CLOSE]` `logger.info`.
- **EXIT (SL/TP auto) — executor.py:276:** append `%s` + `book_slippage_suffix(execution_metadata)`
  to the existing `[TP_HIT]/[SL_HIT]` `logger.info`.

Import `book_slippage_suffix` from `app.fill` into `executor.py`.

### Example output
```
[OPEN] alpha-1-bangoc LONG BTCUSDT.P qty=0.01000000 fill=64012.500000 | book_slip=8.4bps src=live_book state=READY
[CLOSE] alpha-1-v5b 123e... reason=signal qty=0.01 remaining=0.0 raw_fill=64300.0 fill=64287.1 | book_slip=4.0bps src=rest
[SL_HIT] alpha-1-v5b-reverse SHORT ETHUSDT.P stop=3400.0 trigger=3401.0 fill=3402.1 | book_slip=n/a src=fixed_pct(rpc_unavailable)
```

## 5. Error Handling

- Missing / malformed `slippage_bps` in the RPC response → `book_slippage_bps` stays
  `None` → logged as `book_slip=n/a` (never raises).
- The formatter tolerates `{}`, missing keys, and a `None` argument → returns `""`.
- Log formatting must never break the trade path: the suffix is a pure string built from
  already-validated metadata.

## 6. Testing

Pure unit tests, no Redis (mirroring `worker/tests/test_slippage_client.py` /
`test_executor_*`):

- **`test_resolve_populates_book_slippage_bps`** — `FillService.resolve()` with a fake
  `SlippageClient.query` returning `{"source":"live_book","slippage_bps":8.4,
  "avg_exec_price":...,"filled_qty":...,"requested_qty":...,"book_state":"READY"}` →
  `resolution.book_slippage_bps == 8.4`. Second case: `query` returns `fallback_used=True`
  (or `None`) → `book_slippage_bps is None`.
- **`test_book_slippage_suffix`** — three inputs: (a) book-sourced metadata →
  `" | book_slip=8.4bps src=live_book state=READY"`; (b) fallback metadata
  (`book_slippage_bps=None, initial_source="fixed_pct", fallback_reason="rpc_unavailable"`)
  → `" | book_slip=n/a src=fixed_pct(rpc_unavailable)"`; (c) `{}` → `""`.

The three log-line edits are low-risk format strings interpolating the tested suffix; no
separate assertion needed. Existing worker tests must stay green.

## 7. Files Touched

| File | Change |
|---|---|
| `worker/app/slippage_client.py` | `FillResolution.book_slippage_bps` field + populate in `resolve()` |
| `worker/app/fill.py` | `book_slippage_suffix()` helper |
| `worker/app/executor.py` | import helper; add `[OPEN]` log; extend `[CLOSE]` and `[TP_HIT]/[SL_HIT]` logs |
| `worker/tests/test_slippage_client.py` (or new) | `test_resolve_populates_book_slippage_bps` |
| `worker/tests/test_fill.py` (or existing) | `test_book_slippage_suffix` |
