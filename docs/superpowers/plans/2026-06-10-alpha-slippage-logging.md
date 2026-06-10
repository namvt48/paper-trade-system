# Order-Book Slippage Logging (All Alphas) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Log the order-book-computed slippage (`slippage_bps`) on every alpha's entry and exit log line in the paper-trade worker.

**Architecture:** The slippage RPC already returns `slippage_bps` but `FillService.resolve()` discards it. Thread it onto `FillResolution.book_slippage_bps`; add a pure `book_slippage_suffix()` formatter in `worker/app/fill.py`; append the suffix to the three centralized fill-path log points in `executor.py` (a new `[OPEN]` line plus the existing `[CLOSE]` and `[TP_HIT]/[SL_HIT]` lines). App-log only, no DB.

**Tech Stack:** Python 3.12, asyncio, pytest + pytest-asyncio. Run from `/home/namvt/Desktop/quant-space/system/paper-trade-system`; tests live in `worker/tests/`. Branch: `alpha-slippage-logging`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `worker/app/slippage_client.py` | `FillResolution.book_slippage_bps` field + populate it in `FillService.resolve()` | Modify |
| `worker/app/fill.py` | `book_slippage_suffix()` pure formatter | Modify |
| `worker/app/executor.py` | import formatter; add `[OPEN]` log; extend `[CLOSE]` and `[TP_HIT]/[SL_HIT]` logs | Modify |
| `worker/tests/test_slippage_client.py` | `resolve()` populates / nulls `book_slippage_bps` | Modify |
| `worker/tests/test_fill.py` | `book_slippage_suffix()` formatting | Modify |

**Run all tests with:** `python -m pytest worker/tests/ -q` (from the repo root). Single test: `python -m pytest worker/tests/test_fill.py::test_name -v`.

---

## Task 1: Surface `slippage_bps` onto `FillResolution`

**Files:**
- Modify: `worker/app/slippage_client.py` (dataclass `FillResolution` + `FillService.resolve`)
- Test: `worker/tests/test_slippage_client.py`

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_slippage_client.py`:

```python
class _StubQueryClient:
    """Minimal SlippageClient stand-in: query() returns a fixed RPC response."""
    def __init__(self, resp):
        self._resp = resp
    async def query(self, *args, **kwargs):
        return self._resp


@pytest.mark.asyncio
async def test_resolve_populates_book_slippage_bps():
    from app.slippage_client import FillService
    resp = {
        "source": "live_book", "slippage_bps": 8.4, "avg_exec_price": 100.0,
        "reference_price": 100.0, "filled_qty": 1.0, "requested_qty": 1.0,
        "book_state": "READY",
    }
    svc = FillService(_StubQueryClient(resp), slippage_pct=0.05, supported_exchanges={"binance"})
    res = await svc.resolve("binance", "BTCUSDT.P", "LONG", 1.0, ref_price=100.0, is_close=False)
    assert res.book_slippage_bps == 8.4


@pytest.mark.asyncio
async def test_resolve_book_slippage_none_on_fallback():
    from app.slippage_client import FillService
    resp = {
        "source": "fallback", "fallback_used": True, "slippage_bps": 0.0,
        "avg_exec_price": 0.0, "filled_qty": 0.0, "requested_qty": 1.0,
    }
    svc = FillService(_StubQueryClient(resp), slippage_pct=0.05, supported_exchanges={"binance"})
    res = await svc.resolve("binance", "BTCUSDT.P", "LONG", 1.0, ref_price=100.0, is_close=False)
    assert res.book_slippage_bps is None
```

(Confirm `import pytest` is already at the top of the file; it is — the file already has async tests. If not, add it.)

- [ ] **Step 2: Run the tests, verify they FAIL**

Run: `python -m pytest worker/tests/test_slippage_client.py::test_resolve_populates_book_slippage_bps worker/tests/test_slippage_client.py::test_resolve_book_slippage_none_on_fallback -v`
Expected: FAIL — `AttributeError: 'FillResolution' object has no attribute 'book_slippage_bps'`.

- [ ] **Step 3: Add the dataclass field**

In `worker/app/slippage_client.py`, in the `FillResolution` dataclass, add the field immediately after the line `adverse_movement_bps: float = 0.0`:

```python
    book_slippage_bps: float | None = None
```

- [ ] **Step 4: Populate it in `resolve()`**

In `FillService.resolve()`, find the block that builds `resolution = FillResolution(...)` (it ends with `initial_snapshot_age_ms=snapshot_age_ms,` then `)`). Immediately AFTER that closing `)` and BEFORE the line `if not self._latency_model_enabled or resp is None or resp.get("fallback_used"):`, insert:

```python
        if resp is not None and not resp.get("fallback_used"):
            try:
                resolution.book_slippage_bps = float(resp["slippage_bps"])
            except (KeyError, TypeError, ValueError):
                resolution.book_slippage_bps = None
```

This runs before the latency-model early-return, so `book_slippage_bps` is set on both the early-return path and the latency-model path.

- [ ] **Step 5: Run the tests, verify they PASS**

Run: `python -m pytest worker/tests/test_slippage_client.py -v`
Expected: PASS (the 2 new tests + all existing ones).

- [ ] **Step 6: Commit**

```bash
git add worker/app/slippage_client.py worker/tests/test_slippage_client.py
git commit -m "feat(paper-trade): surface RPC slippage_bps onto FillResolution"
```

---

## Task 2: `book_slippage_suffix` formatter

**Files:**
- Modify: `worker/app/fill.py`
- Test: `worker/tests/test_fill.py`

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_fill.py`:

```python
def test_book_slippage_suffix_book_sourced():
    from app.fill import book_slippage_suffix
    md = {"execution": {
        "book_slippage_bps": 8.4, "initial_source": "live_book", "initial_book_state": "READY",
    }}
    assert book_slippage_suffix(md) == " | book_slip=8.4bps src=live_book state=READY"


def test_book_slippage_suffix_fallback():
    from app.fill import book_slippage_suffix
    md = {"execution": {
        "book_slippage_bps": None, "initial_source": "fixed_pct", "fallback_reason": "rpc_unavailable",
    }}
    assert book_slippage_suffix(md) == " | book_slip=n/a src=fixed_pct(rpc_unavailable)"


def test_book_slippage_suffix_empty():
    from app.fill import book_slippage_suffix
    assert book_slippage_suffix({}) == ""
    assert book_slippage_suffix(None) == ""
```

- [ ] **Step 2: Run the tests, verify they FAIL**

Run: `python -m pytest worker/tests/test_fill.py::test_book_slippage_suffix_book_sourced worker/tests/test_fill.py::test_book_slippage_suffix_fallback worker/tests/test_fill.py::test_book_slippage_suffix_empty -v`
Expected: FAIL — `ImportError: cannot import name 'book_slippage_suffix'`.

- [ ] **Step 3: Implement the formatter**

Append to `worker/app/fill.py`:

```python
def book_slippage_suffix(execution_metadata: dict | None) -> str:
    """Render the order-book slippage as a log suffix from an executor's
    execution_metadata ({"execution": <FillResolution asdict>} or {}). Returns an
    empty string when there is no execution metadata (e.g. a plain float fill)."""
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

- [ ] **Step 4: Run the tests, verify they PASS**

Run: `python -m pytest worker/tests/test_fill.py -v`
Expected: PASS (3 new + existing).

- [ ] **Step 5: Commit**

```bash
git add worker/app/fill.py worker/tests/test_fill.py
git commit -m "feat(paper-trade): add book_slippage_suffix log formatter"
```

---

## Task 3: Wire the suffix into the three log points

**Files:**
- Modify: `worker/app/executor.py`

- [ ] **Step 1: Extend the import**

In `worker/app/executor.py`, change:

```python
from app.fill import fixed_pct_fill
```

to:

```python
from app.fill import fixed_pct_fill, book_slippage_suffix
```

- [ ] **Step 2: Add the `[OPEN]` log line**

In `Executor.process_open`, the method currently ends with:

```python
        return {"position_id": position_id, "fill_price": fill_price}
```

Insert a log line immediately BEFORE that `return` (at this point both `fill_price` and `execution_metadata` are already in scope from the `fill_price, execution_metadata = self._fill_value_and_metadata(fill_price)` line earlier in the method):

```python
        logger.info(
            "[OPEN] %s %s %s qty=%.8f fill=%.6f%s",
            signal.alpha_id, signal.side, signal.symbol, signal.qty, fill_price,
            book_slippage_suffix(execution_metadata),
        )
        return {"position_id": position_id, "fill_price": fill_price}
```

- [ ] **Step 3: Extend the `[CLOSE]` log line**

In `Executor.process_close`, replace:

```python
        logger.info(
            "[CLOSE] %s %s reason=%s qty=%.8f remaining=%.8f raw_fill=%.6f fill=%.6f",
            signal.alpha_id, signal.position_id, signal.reason, close_qty,
            remaining_qty, raw_exit, exit_price,
        )
```

with:

```python
        logger.info(
            "[CLOSE] %s %s reason=%s qty=%.8f remaining=%.8f raw_fill=%.6f fill=%.6f%s",
            signal.alpha_id, signal.position_id, signal.reason, close_qty,
            remaining_qty, raw_exit, exit_price,
            book_slippage_suffix(execution_metadata),
        )
```

(`execution_metadata` is already created earlier in `process_close` via `_fill_value_and_metadata` and used in `close_metadata`.)

- [ ] **Step 4: Extend the `[TP_HIT]/[SL_HIT]` log line**

In `Executor.check_tpsl_hits`, replace:

```python
                logger.info(
                    "[%s] %s %s %s stop=%.6f trigger=%.6f fill=%.6f",
                    reason, pos["alpha_id"], pos["side"], pos["symbol"],
                    stop_price, current_price, fill_exit,
                )
```

with:

```python
                logger.info(
                    "[%s] %s %s %s stop=%.6f trigger=%.6f fill=%.6f%s",
                    reason, pos["alpha_id"], pos["side"], pos["symbol"],
                    stop_price, current_price, fill_exit,
                    book_slippage_suffix(execution_metadata),
                )
```

(`execution_metadata` is in scope from `fill_exit, execution_metadata = self._fill_value_and_metadata(fill_exit)` just above.)

- [ ] **Step 5: Verify import is clean and existing executor tests pass**

Run: `python -c "import sys; sys.path.insert(0, 'worker'); import app.executor"` (expect no error).
Run: `python -m pytest worker/tests/test_executor.py worker/tests/test_executor_fill_price.py worker/tests/test_executor_tpsl_book.py -v`
Expected: PASS (no behavior change — the log lines only add a string suffix; no test asserts the exact log text).

- [ ] **Step 6: Commit**

```bash
git add worker/app/executor.py
git commit -m "feat(paper-trade): log order-book slippage on entry/exit for all alphas"
```

---

## Task 4: Full worker suite regression

**Files:** none (verification only)

- [ ] **Step 1: Run the whole worker suite**

Run: `python -m pytest worker/tests/ -q`
Expected: PASS, except any pre-existing failures unrelated to this change (e.g. a known `.env`/`REDIS_URL` config-test failure documented in prior paper-trade work). Note the count; no *new* failures introduced by these three tasks.

- [ ] **Step 2: Eyeball a sample log shape (optional sanity)**

Run:
```bash
python -c "
import sys; sys.path.insert(0, 'worker')
from app.fill import book_slippage_suffix
print('OPEN line ->', '[OPEN] alpha-x LONG BTCUSDT.P qty=0.01000000 fill=64012.500000' + book_slippage_suffix({'execution': {'book_slippage_bps': 8.4, 'initial_source': 'live_book', 'initial_book_state': 'READY'}}))
print('fallback ->', book_slippage_suffix({'execution': {'book_slippage_bps': None, 'initial_source': 'fixed_pct', 'fallback_reason': 'rpc_unavailable'}}))
"
```
Expected:
```
OPEN line -> [OPEN] alpha-x LONG BTCUSDT.P qty=0.01000000 fill=64012.500000 | book_slip=8.4bps src=live_book state=READY
fallback ->  | book_slip=n/a src=fixed_pct(rpc_unavailable)
```

---

## Self-Review Notes

- **Spec coverage:** §4.1 (field + populate) → Task 1; §4.2 (formatter) → Task 2; §4.3 (three log points, new `[OPEN]` + extended `[CLOSE]`/`[TP_HIT]`) → Task 3; §6 (tests) → Tasks 1–2 tests + Task 4 regression. §5 (error handling: missing/malformed `slippage_bps` → `None` → `n/a`; formatter tolerates `{}`/`None`) → covered by Task 1 Step 4 try/except and Task 2 tests.
- **Type consistency:** `book_slippage_bps` (field), `book_slippage_suffix` (function), `execution_metadata` nesting `{"execution": asdict}` — names match across all tasks and the real code (`_fill_value_and_metadata` returns `{"execution": fill_price.metadata()}`).
- **No new DB/migration, no second log stream** — matches the spec's Non-Goals.
