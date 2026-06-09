# Order Book (L2) — Paper-Trade Integration Plan (decision b1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make paper-trade execution book-accurate: fills (entry, exit, TP/SL) use the MDS slippage RPC (walk-the-book average executed price), and worker TP/SL triggering uses the executable-side book price (`ob_exec`) when available — while keeping ticker as an always-on safety net and fixed-pct as the fill fallback.

**Architecture:** A thin async `SlippageClient` calls the MDS RPC (`LPUSH` request + `BLPOP` response, short timeout). A pure `resolve_fill_price` blends the RPC result with fixed-pct fallback. The executor accepts a pre-resolved `fill_price` (computed **outside** the DB transaction) and a side-aware price source for TP/SL. The worker adds an `ob_exec` subscriber (layered on top of the existing ticker subscriber) and an `orderbook:subscribe` sync loop.

**Tech Stack:** Python 3, asyncio, `redis.asyncio` (worker already uses it), stdlib `json`/`uuid`, pytest + pytest-asyncio, `fakeredis` (test-only). No production deps added.

**Depends on:** MDS plan `market-data-service/docs/superpowers/plans/2026-06-09-orderbook-l2-mds.md` (provides the RPC + `ob_exec` + `orderbook:subscribe` contract). This plan can be coded against that contract independently; end-to-end verification needs MDS running.

**Spec:** `market-data-service/docs/superpowers/specs/2026-06-09-orderbook-l2-integration-design.md` (§8).

**Conventions:** Worker code in `worker/app/`, tests in `worker/tests/`, imports as `app.…`, run with `cd worker && python -m pytest tests/<file> -v`. Commit after each task.

**Backward-compat rule:** Existing executor tests call `process_open(signal)`, `process_close(...)`, and `check_tpsl_hits({"SYM": price})`. Every change keeps these working by making new parameters optional with defaults that reproduce today's behavior.

---

### Task 1: Pure fill resolution (`fill.py`) + DRY `_apply_slippage`

**Files:**
- Create: `worker/app/fill.py`
- Modify: `worker/app/executor.py` (`_apply_slippage` delegates)
- Test: `worker/tests/test_fill.py`

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_fill.py`:

```python
from app.fill import fixed_pct_fill, resolve_fill_price


def test_fixed_pct_fill_long_open_adds():
    # entry 95000, slippage_pct 0.1 -> slip 9.5 -> 95009.5
    assert fixed_pct_fill(95000.0, "LONG", 0.1, is_close=False) == 95009.5


def test_fixed_pct_fill_long_close_subtracts():
    assert fixed_pct_fill(95000.0, "LONG", 0.1, is_close=True) == 94990.5


def test_fixed_pct_fill_short_open_subtracts():
    assert fixed_pct_fill(95000.0, "SHORT", 0.1, is_close=False) == 94990.5


def test_resolve_uses_avg_when_fully_filled():
    resp = {"fallback_used": False, "filled_qty": 1.0, "requested_qty": 1.0, "avg_exec_price": 101.0}
    assert resolve_fill_price(resp, 100.0, "LONG", False, 0.5) == 101.0


def test_resolve_falls_back_on_none():
    # ref 100, LONG open, pct 0.5 -> slip 0.05 -> 100.05
    assert resolve_fill_price(None, 100.0, "LONG", False, 0.5) == 100.05


def test_resolve_falls_back_on_flag():
    resp = {"fallback_used": True}
    assert resolve_fill_price(resp, 100.0, "LONG", False, 0.5) == 100.05


def test_resolve_blends_partial_fill():
    # filled 1 @ 100 (avg), remainder 1 @ fixed (LONG open pct 0 -> 200 ref)
    resp = {"fallback_used": False, "filled_qty": 1.0, "requested_qty": 2.0, "avg_exec_price": 100.0}
    # ref 200, pct 0 -> fixed=200 ; blend = (1*100 + 1*200)/2 = 150
    assert resolve_fill_price(resp, 200.0, "LONG", False, 0.0) == 150.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_fill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.fill'`

- [ ] **Step 3: Implement `fill.py`**

Create `worker/app/fill.py`:

```python
from __future__ import annotations

_EPS = 1e-12


def fixed_pct_fill(price: float, position_side: str, slippage_pct: float, is_close: bool) -> float:
    """Fixed-percentage slippage model (the legacy fallback).

    slippage_pct is in per-mille tenths as used today: slip = price * (slippage_pct / 1000).
    """
    slip = price * (slippage_pct / 1000.0)
    if position_side.upper() == "LONG":
        return (price - slip) if is_close else (price + slip)
    return (price + slip) if is_close else (price - slip)


def resolve_fill_price(
    resp: dict | None,
    ref_price: float,
    position_side: str,
    is_close: bool,
    slippage_pct: float,
) -> float:
    """Turn an MDS slippage RPC response into a fill price.

    Falls back to fixed-pct when the RPC is unavailable/fallback; blends the filled
    portion (book avg) with fixed-pct on any unfilled remainder.
    """
    if resp is None or resp.get("fallback_used"):
        return fixed_pct_fill(ref_price, position_side, slippage_pct, is_close)
    filled = float(resp.get("filled_qty", 0.0))
    requested = float(resp.get("requested_qty", 0.0))
    avg = float(resp.get("avg_exec_price", 0.0))
    if filled <= _EPS or avg <= 0.0:
        return fixed_pct_fill(ref_price, position_side, slippage_pct, is_close)
    if filled >= requested - _EPS:
        return avg
    remainder = requested - filled
    fixed_price = fixed_pct_fill(ref_price, position_side, slippage_pct, is_close)
    return (filled * avg + remainder * fixed_price) / requested
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd worker && python -m pytest tests/test_fill.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Make `_apply_slippage` delegate (DRY)**

In `worker/app/executor.py`, add the import near the top:

```python
from app.fill import fixed_pct_fill
```

Replace the body of `_apply_slippage`:

```python
    def _apply_slippage(self, price: float, side: str, is_close: bool = False) -> float:
        return fixed_pct_fill(price, side, self.slippage_pct, is_close)
```

- [ ] **Step 6: Run the existing executor tests (no regression)**

Run: `cd worker && python -m pytest tests/test_executor.py -v`
Expected: PASS (all existing tests, incl. `test_process_open_with_slippage`)

- [ ] **Step 7: Commit**

```bash
git add worker/app/fill.py worker/app/executor.py worker/tests/test_fill.py
git commit -m "feat(paper-trade): pure fill resolution + DRY fixed-pct slippage"
```

---

### Task 2: Slippage RPC client + `FillService`

**Files:**
- Create: `worker/app/slippage_client.py`
- Test: `worker/tests/test_slippage_client.py`

- [ ] **Step 1: Ensure async `fakeredis` is available**

Run: `python -c "import fakeredis.aioredis" 2>/dev/null && echo OK || pip install 'fakeredis>=2'`
Expected: `OK` (install if missing — test-only).

- [ ] **Step 2: Write the failing test**

Create `worker/tests/test_slippage_client.py`:

```python
import json

import fakeredis.aioredis
import pytest

from app.slippage_client import SlippageClient, FillService, order_side_for


def test_order_side_mapping():
    assert order_side_for("LONG", is_close=False) == "BUY"
    assert order_side_for("SHORT", is_close=False) == "SELL"
    assert order_side_for("LONG", is_close=True) == "SELL"
    assert order_side_for("SHORT", is_close=True) == "BUY"


@pytest.mark.asyncio
async def test_query_returns_response_when_present():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = SlippageClient(r)
    # Pre-seed the response a server would have written.
    await r.lpush("orderbook:slip:resp:rid-1", json.dumps({"avg_exec_price": 101.0, "fallback_used": False}))
    resp = await client.query("binance", "BTCUSDT", "BUY", 1.0, fallback_pct=0.5,
                              timeout=1, request_id="rid-1")
    assert resp["avg_exec_price"] == 101.0
    # The request was enqueued for the server.
    raw = await r.lpop("orderbook:slip:req:binance")
    assert json.loads(raw)["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_query_returns_none_on_timeout():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = SlippageClient(r)
    resp = await client.query("binance", "BTCUSDT", "BUY", 1.0, timeout=1, request_id="rid-x")
    assert resp is None


@pytest.mark.asyncio
async def test_fill_service_uses_book_avg():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.lpush("orderbook:slip:resp:rid-2", json.dumps(
        {"fallback_used": False, "filled_qty": 1.0, "requested_qty": 1.0, "avg_exec_price": 101.0}))
    svc = FillService(SlippageClient(r), slippage_pct=0.5, timeout=1)
    price = await svc.resolve("binance", "BTCUSDT", "LONG", 1.0, ref_price=100.0,
                              is_close=False, request_id="rid-2")
    assert price == 101.0


@pytest.mark.asyncio
async def test_fill_service_falls_back_on_timeout():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    svc = FillService(SlippageClient(r), slippage_pct=0.5, timeout=1)
    # no response seeded -> timeout -> fixed-pct (LONG open ref 100 pct 0.5 -> 100.05)
    price = await svc.resolve("binance", "BTCUSDT", "LONG", 1.0, ref_price=100.0,
                              is_close=False, request_id="rid-y")
    assert price == 100.05
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_slippage_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.slippage_client'`

- [ ] **Step 4: Implement `slippage_client.py`**

Create `worker/app/slippage_client.py`:

```python
from __future__ import annotations

import json
import logging
import uuid

from app.fill import resolve_fill_price

logger = logging.getLogger(__name__)


def order_side_for(position_side: str, is_close: bool) -> str:
    """Map a position side + open/close to the order side the book is walked on.

    Open LONG -> BUY (consume asks); Open SHORT -> SELL (consume bids).
    Closing flips the side.
    """
    is_long = position_side.upper() == "LONG"
    if is_close:
        is_long = not is_long
    return "BUY" if is_long else "SELL"


class SlippageClient:
    """Async client for the MDS slippage RPC: LPUSH request, BLPOP response."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def query(self, exchange: str, symbol: str, side: str, qty: float,
                    fallback_pct: float = 0.0, timeout: float = 0.2,
                    request_id: str | None = None) -> dict | None:
        rid = request_id or uuid.uuid4().hex
        req = {
            "request_id": rid,
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "fallback_pct": fallback_pct,
        }
        resp_key = f"orderbook:slip:resp:{rid}"
        try:
            await self._redis.lpush(f"orderbook:slip:req:{exchange}", json.dumps(req))
            item = await self._redis.blpop([resp_key], timeout=timeout)
        except Exception as exc:
            logger.warning("[SLIP-RPC] query failed for %s: %s", symbol, exc)
            return None
        if not item:
            return None
        _, raw = item
        try:
            return json.loads(raw)
        except Exception:
            return None


class FillService:
    """Resolves a fill price: RPC walk if available, else fixed-pct fallback."""

    def __init__(self, client: SlippageClient, slippage_pct: float, timeout: float = 0.2) -> None:
        self._client = client
        self._slippage_pct = slippage_pct
        self._timeout = timeout

    async def resolve(self, exchange: str, symbol: str, position_side: str, qty: float,
                      ref_price: float, is_close: bool, request_id: str | None = None) -> float:
        order_side = order_side_for(position_side, is_close)
        resp = await self._client.query(
            exchange, symbol, order_side, qty,
            fallback_pct=self._slippage_pct, timeout=self._timeout, request_id=request_id,
        )
        return resolve_fill_price(resp, ref_price, position_side, is_close, self._slippage_pct)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd worker && python -m pytest tests/test_slippage_client.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add worker/app/slippage_client.py worker/tests/test_slippage_client.py
git commit -m "feat(paper-trade): MDS slippage RPC client + FillService"
```

---

### Task 3: Executor accepts a pre-resolved `fill_price`

**Files:**
- Modify: `worker/app/executor.py` (`process_open`, `process_close`)
- Test: `worker/tests/test_executor_fill_price.py`

The RPC must run **outside** the DB transaction (spec §8.2), so the executor takes an
already-resolved price. `fill_price=None` preserves today's behavior.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_executor_fill_price.py`:

```python
import pytest

from app.db import Database
from app.executor import Executor
from app.models import OpenSignal, CloseSignal, SignalType


@pytest.mark.asyncio
async def test_open_uses_injected_fill_price(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.1)  # would add slippage if not overridden
    sig = OpenSignal(type=SignalType.OPEN, alpha_id="a", signal_id="s1", symbol="BTCUSDT",
                     side="LONG", entry=95000.0, qty=0.01, timestamp="2026-05-22T10:00:00Z")
    result = await ex.process_open(sig, fill_price=95123.0)
    assert result["fill_price"] == 95123.0
    await db.close()


@pytest.mark.asyncio
async def test_open_without_fill_price_keeps_fixed_pct(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.1)
    sig = OpenSignal(type=SignalType.OPEN, alpha_id="a", signal_id="s2", symbol="BTCUSDT",
                     side="LONG", entry=95000.0, qty=0.01, timestamp="2026-05-22T10:00:00Z")
    result = await ex.process_open(sig)  # no fill_price -> fixed-pct (95009.5)
    assert result["fill_price"] == pytest.approx(95009.5)
    await db.close()


@pytest.mark.asyncio
async def test_close_uses_injected_fill_price(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.0)
    open_sig = OpenSignal(type=SignalType.OPEN, alpha_id="a", signal_id="s3", symbol="BTCUSDT",
                          side="LONG", entry=95000.0, qty=0.01, timestamp="2026-05-22T10:00:00Z")
    opened = await ex.process_open(open_sig)
    close_sig = CloseSignal(type=SignalType.CLOSE, alpha_id="a", signal_id="s4",
                            position_id=opened["position_id"], reason="SIGNAL",
                            timestamp="2026-05-22T11:00:00Z", exit_price=96000.0)
    result = await ex.process_close(close_sig, fill_price=95888.0)
    assert result["exit_price"] == 95888.0
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_executor_fill_price.py -v`
Expected: FAIL — `TypeError: process_open() got an unexpected keyword argument 'fill_price'`

- [ ] **Step 3: Add the optional parameter to `process_open`**

In `worker/app/executor.py`, change the `process_open` signature and the fill line. Signature:

```python
    async def process_open(self, signal: OpenSignal, fill_price: float | None = None) -> dict:
```

Replace the existing fill computation line:

```python
        fill_price = self._apply_slippage(signal.entry, signal.side)
```

with:

```python
        fill_price = (
            fill_price if fill_price is not None
            else self._apply_slippage(signal.entry, signal.side)
        )
```

- [ ] **Step 4: Add the optional parameter to `process_close`**

Change the `process_close` signature:

```python
    async def process_close(self, signal: CloseSignal, fill_price: float | None = None) -> dict:
```

Replace the existing exit-price computation:

```python
        raw_exit = signal.exit_price or pos["entry_price"]
        exit_price = self._apply_slippage(raw_exit, pos["side"], is_close=True)
```

with:

```python
        raw_exit = signal.exit_price or pos["entry_price"]
        exit_price = (
            fill_price if fill_price is not None
            else self._apply_slippage(raw_exit, pos["side"], is_close=True)
        )
```

- [ ] **Step 5: Run tests to verify they pass (new + existing)**

Run: `cd worker && python -m pytest tests/test_executor_fill_price.py tests/test_executor.py -v`
Expected: PASS (new 3 + all existing executor tests)

- [ ] **Step 6: Commit**

```bash
git add worker/app/executor.py worker/tests/test_executor_fill_price.py
git commit -m "feat(paper-trade): executor accepts pre-resolved fill_price (RPC outside txn)"
```

---

### Task 4: Side-aware TP/SL trigger + RPC fill in `check_tpsl_hits`

**Files:**
- Modify: `worker/app/executor.py` (`check_tpsl_hits`)
- Test: `worker/tests/test_executor_tpsl_book.py`

`check_tpsl_hits` keeps accepting a `dict[str, float]` (legacy) **or** a callable
`price_fn(symbol, side) -> float | None` (b1 side-aware). An optional async
`fill_resolver(exchange, symbol, position_side, qty, ref_price, is_close) -> float`
supplies the book-walked exit fill; absent, it uses fixed-pct.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_executor_tpsl_book.py`:

```python
import pytest

from app.db import Database
from app.executor import Executor
from app.models import OpenSignal, SignalType


async def _open(ex, side, entry, tp=None, sl=None):
    sig = OpenSignal(type=SignalType.OPEN, alpha_id="a", signal_id="s", symbol="BTCUSDT",
                     side=side, entry=entry, qty=0.01, tp=tp, sl=sl,
                     timestamp="2026-05-22T10:00:00Z")
    return await ex.process_open(sig)


@pytest.mark.asyncio
async def test_callable_price_fn_receives_side(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.0)
    await _open(ex, "LONG", 95000.0, tp=97000.0)
    seen = {}

    def price_fn(symbol, side):
        seen["side"] = side
        return 97500.0  # above TP -> hit

    hits = await ex.check_tpsl_hits(price_fn)
    assert seen["side"] == "LONG"
    assert len(hits) == 1
    await db.close()


@pytest.mark.asyncio
async def test_fill_resolver_overrides_exit(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.0)
    await _open(ex, "LONG", 95000.0, tp=97000.0)

    async def fill_resolver(exchange, symbol, side, qty, ref_price, is_close):
        assert is_close is True
        assert side == "LONG"
        return 97777.0

    hits = await ex.check_tpsl_hits({"BTCUSDT": 97500.0}, fill_resolver=fill_resolver)
    assert hits[0]["exit_price"] == 97777.0
    await db.close()


@pytest.mark.asyncio
async def test_legacy_dict_still_works(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.0)
    await _open(ex, "LONG", 95000.0, tp=97000.0)
    hits = await ex.check_tpsl_hits({"BTCUSDT": 97500.0})  # no resolver -> fixed-pct
    assert hits[0]["exit_price"] == pytest.approx(97500.0)  # pct 0
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_executor_tpsl_book.py -v`
Expected: FAIL — `TypeError: check_tpsl_hits() ... ` (callable not supported / no `fill_resolver` kwarg)

- [ ] **Step 3: Update `check_tpsl_hits`**

In `worker/app/executor.py`, add a small normalizer method and rewrite `check_tpsl_hits`.
Add this helper to the `Executor` class:

```python
    @staticmethod
    def _as_price_fn(price_source):
        if callable(price_source):
            return price_source
        return lambda symbol, side: price_source.get(symbol)
```

Replace the `check_tpsl_hits` method signature and the `current_price` / fill lines. New
signature:

```python
    async def check_tpsl_hits(self, price_source, fill_resolver=None) -> list[dict]:
        price_fn = self._as_price_fn(price_source)
        positions = await self.db.get_positions_with_tpsl()
        hits = []

        for pos in positions:
            current_price = price_fn(pos["symbol"], pos["side"])
            if current_price is None:
                continue
```

(Keep the existing TP/SL comparison block unchanged — it already uses `current_price`,
`pos["side"]`, `pos["tp"]`, `pos["sl"]`.)

Then replace the fill computation inside the `if closed and exit_price is not None:` block:

```python
            if closed and exit_price is not None:
                if fill_resolver is None:
                    fill_exit = self._apply_slippage(exit_price, pos["side"], is_close=True)
                else:
                    fill_exit = await fill_resolver(
                        pos.get("exchange", "binance"), pos["symbol"], pos["side"],
                        pos["qty"], exit_price, True,
                    )
```

(The rest of that block — building `close_meta`, calling `self.db.close_position`, appending
to `hits`, logging — stays the same, still using `fill_exit`.)

> Assumption to verify: rows from `db.get_positions_with_tpsl()` expose `symbol`, `side`,
> `qty`, `tp`, `sl`, `position_id`. `exchange` may be absent → defaulted to `"binance"`.

- [ ] **Step 4: Run tests to verify they pass (new + existing)**

Run: `cd worker && python -m pytest tests/test_executor_tpsl_book.py tests/test_executor.py -v`
Expected: PASS (new 3 + all existing TP/SL tests, which pass dicts and no resolver)

- [ ] **Step 5: Commit**

```bash
git add worker/app/executor.py worker/tests/test_executor_tpsl_book.py
git commit -m "feat(paper-trade): side-aware TP/SL trigger + RPC exit fill"
```

---

### Task 5: Worker `ob_exec` cache + subscriber (ticker stays the baseline)

**Files:**
- Create: `worker/app/ob_exec.py`
- Test: `worker/tests/test_ob_exec_cache.py`

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_ob_exec_cache.py`:

```python
from app.ob_exec import ObExecCache, make_exit_price_fn


class _TickerCache:
    def __init__(self, prices):
        self._p = prices

    def get_prices(self, symbols=None):
        if symbols is None:
            return dict(self._p)
        return {s: self._p[s] for s in symbols if s in self._p}


def test_side_price_ready_long_uses_bid():
    c = ObExecCache()
    c.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="READY")
    assert c.side_price("BTCUSDT", "LONG") == 100.0
    assert c.side_price("BTCUSDT", "SHORT") == 101.0


def test_side_price_returns_none_when_not_ready():
    c = ObExecCache()
    c.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="STALE")
    assert c.side_price("BTCUSDT", "LONG") is None


def test_exit_price_fn_prefers_book_then_ticker():
    ob = ObExecCache()
    ob.update("BTCUSDT", best_bid=100.0, best_ask=101.0, state="READY")
    ticker = _TickerCache({"BTCUSDT": 100.5, "ETHUSDT": 3000.0})
    fn = make_exit_price_fn(ob, ticker)
    assert fn("BTCUSDT", "LONG") == 100.0       # book best_bid
    assert fn("ETHUSDT", "LONG") == 3000.0      # ticker fallback (no book)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_ob_exec_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ob_exec'`

- [ ] **Step 3: Implement `ob_exec.py`**

Create `worker/app/ob_exec.py`:

```python
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


class ObExecCache:
    """Latest executable-side book prices per symbol from the ob_exec feed."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, float, str]] = {}  # symbol -> (bid, ask, state)

    def update(self, symbol: str, best_bid: float, best_ask: float, state: str) -> None:
        self._data[symbol] = (best_bid, best_ask, state)

    def side_price(self, symbol: str, position_side: str) -> float | None:
        item = self._data.get(symbol)
        if item is None:
            return None
        bid, ask, state = item
        if state != "READY":
            return None
        return bid if position_side.upper() == "LONG" else ask


def make_exit_price_fn(ob_cache: ObExecCache, ticker_cache):
    """Side-aware price provider: book best bid/ask when READY, else ticker mid."""

    def price_fn(symbol: str, position_side: str) -> float | None:
        book_price = ob_cache.side_price(symbol, position_side)
        if book_price is not None:
            return book_price
        return ticker_cache.get_prices([symbol]).get(symbol)

    return price_fn


async def run_ob_exec_subscriber(cache: ObExecCache, connect_redis, exchange: str = "binance") -> None:
    """Pattern-subscribe ob_exec:{exchange}:* and keep the cache fresh.

    MDS only publishes ob_exec for symbols it has WS depth for (i.e. the open-position
    symbols this worker subscribed), so the pattern naturally scopes to those.
    """
    redis_client = await connect_redis()
    pubsub = redis_client.pubsub()
    pattern = f"ob_exec:{exchange}:*"
    await pubsub.psubscribe(pattern)
    logger.info("[OB-EXEC] psubscribed %s", pattern)
    try:
        while True:
            try:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not msg or msg.get("type") != "pmessage":
                    continue
                data = json.loads(msg["data"])
                symbol = data.get("symbol")
                if not symbol:
                    continue
                cache.update(
                    symbol,
                    best_bid=float(data.get("best_bid", 0.0)),
                    best_ask=float(data.get("best_ask", 0.0)),
                    state=data.get("book_state", ""),
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[OB-EXEC] subscriber error: %s", exc)
                await asyncio.sleep(5)
    finally:
        await pubsub.punsubscribe()
        await pubsub.aclose()
        await redis_client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd worker && python -m pytest tests/test_ob_exec_cache.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add worker/app/ob_exec.py worker/tests/test_ob_exec_cache.py
git commit -m "feat(paper-trade): ob_exec cache + subscriber + side-aware price fn"
```

---

### Task 6: `orderbook:subscribe` sync publisher (poll + event-driven)

**Files:**
- Create: `worker/app/ob_subscribe.py`
- Test: `worker/tests/test_ob_subscribe.py`

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_ob_subscribe.py`:

```python
import json

import fakeredis.aioredis
import pytest

from app.ob_subscribe import publish_subscribe, publish_sync


@pytest.mark.asyncio
async def test_publish_sync_payload():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe("orderbook:subscribe:binance")
    await pubsub.get_message(timeout=1)  # drain ack

    await publish_sync(r, "binance", "worker-1", ["BTCUSDT", "ETHUSDT"])

    msg = await pubsub.get_message(timeout=1)
    payload = json.loads(msg["data"])
    assert payload["action"] == "sync"
    assert payload["consumer_id"] == "worker-1"
    assert sorted(payload["symbols"]) == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.asyncio
async def test_publish_subscribe_single_symbol():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe("orderbook:subscribe:binance")
    await pubsub.get_message(timeout=1)

    await publish_subscribe(r, "binance", "worker-1", "SOLUSDT")

    msg = await pubsub.get_message(timeout=1)
    payload = json.loads(msg["data"])
    assert payload["action"] == "subscribe"
    assert payload["symbols"] == ["SOLUSDT"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_ob_subscribe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ob_subscribe'`

- [ ] **Step 3: Implement `ob_subscribe.py`**

Create `worker/app/ob_subscribe.py`:

```python
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


def _channel(exchange: str) -> str:
    return f"orderbook:subscribe:{exchange}"


async def publish_sync(redis_client, exchange: str, consumer_id: str, symbols: list[str]) -> None:
    await redis_client.publish(
        _channel(exchange),
        json.dumps({"consumer_id": consumer_id, "action": "sync", "symbols": list(symbols)}),
    )


async def publish_subscribe(redis_client, exchange: str, consumer_id: str, symbol: str) -> None:
    await redis_client.publish(
        _channel(exchange),
        json.dumps({"consumer_id": consumer_id, "action": "subscribe", "symbols": [symbol]}),
    )


async def run_orderbook_sync_loop(db, redis_client, consumer_id: str,
                                  exchange: str = "binance", interval: float = 5.0) -> None:
    """Periodically tell MDS which open-position symbols need a depth book."""
    while True:
        try:
            await asyncio.sleep(interval)
            symbols = await db.get_symbols_with_open_positions()
            await publish_sync(redis_client, exchange, consumer_id, symbols)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("[OB-SUB] sync loop error: %s", exc)
            await asyncio.sleep(5)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd worker && python -m pytest tests/test_ob_subscribe.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add worker/app/ob_subscribe.py worker/tests/test_ob_subscribe.py
git commit -m "feat(paper-trade): orderbook subscribe sync publisher"
```

---

### Task 7: Wire into the worker (`main.py` + `config.py`)

**Files:**
- Modify: `worker/app/config.py`
- Modify: `worker/app/main.py`
- Test: `worker/tests/test_main_fill_wiring.py`

- [ ] **Step 1: Add config**

In `worker/app/config.py`, add fields to `Settings`:

```python
    ENABLE_ORDERBOOK_SLIPPAGE: bool = True
    ORDERBOOK_EXCHANGE: str = "binance"
    SLIPPAGE_RPC_TIMEOUT: float = 0.2
    ORDERBOOK_SYNC_INTERVAL: float = 5.0
```

- [ ] **Step 2: Write the failing test (fill resolved before transaction)**

Create `worker/tests/test_main_fill_wiring.py`:

```python
import pytest

from app.db import Database
from app.executor import Executor
from app.main import process_signal_message


class _FillService:
    def __init__(self, price):
        self.price = price
        self.calls = []

    async def resolve(self, exchange, symbol, position_side, qty, ref_price, is_close, request_id=None):
        self.calls.append((symbol, position_side, is_close))
        return self.price


@pytest.mark.asyncio
async def test_open_signal_uses_fill_service(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.1)
    fs = _FillService(price=95222.0)
    data = {"type": "OPEN", "alpha_id": "a", "signal_id": "s1", "symbol": "BTCUSDT",
            "side": "LONG", "entry": "95000.0", "qty": "0.01", "timestamp": "2026-05-22T10:00:00Z"}
    result = await process_signal_message(data, db, ex, fill_service=fs)
    assert result["fill_price"] == 95222.0
    assert fs.calls and fs.calls[0][0] == "BTCUSDT"
    await db.close()


@pytest.mark.asyncio
async def test_open_signal_without_fill_service_is_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.1)
    data = {"type": "OPEN", "alpha_id": "a", "signal_id": "s2", "symbol": "BTCUSDT",
            "side": "LONG", "entry": "95000.0", "qty": "0.01", "timestamp": "2026-05-22T10:00:00Z"}
    result = await process_signal_message(data, db, ex)  # no fill_service
    assert result["fill_price"] == pytest.approx(95009.5)
    await db.close()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_main_fill_wiring.py -v`
Expected: FAIL — `TypeError: process_signal_message() got an unexpected keyword argument 'fill_service'`

- [ ] **Step 4: Resolve the fill before the transaction in `process_signal_message`**

In `worker/app/main.py`, add imports near the top:

```python
from app.models import SignalType, parse_signal, RegisterColumnsSignal
from app.ob_exec import ObExecCache, make_exit_price_fn, run_ob_exec_subscriber
from app.ob_subscribe import publish_subscribe, run_orderbook_sync_loop
from app.slippage_client import SlippageClient, FillService
```

(The first line replaces the existing `from app.models import …`.)

Replace `process_signal_message` with the version that resolves fills outside the transaction:

```python
async def process_signal_message(data: dict, db: Database, executor: Executor,
                                  fill_service=None) -> dict | None:
    signal_id = data.get("signal_id", "unknown")
    alpha_id = data.get("alpha_id", "unknown")
    signal_type = data.get("type", "unknown")

    try:
        signal = parse_signal(data)
    except Exception as exc:
        logger.error("Error parsing signal %s: %s", signal_id, exc)
        async with db.transaction():
            await db.log_signal(signal_id=signal_id, alpha_id=alpha_id,
                                signal_type=signal_type, payload=json.dumps(data))
            await db.mark_signal_processed(signal_id, error=str(exc))
        return None

    # Resolve the book-walked fill price OUTSIDE the DB transaction (spec §8.2): an RPC
    # BLPOP must never be held inside the SQLite writer lock.
    fill_price = None
    if fill_service is not None:
        try:
            if signal.type == SignalType.OPEN:
                fill_price = await fill_service.resolve(
                    signal.exchange, signal.symbol, signal.side, signal.qty,
                    ref_price=signal.entry, is_close=False,
                )
            elif signal.type == SignalType.CLOSE:
                pos = await db.get_position(signal.position_id)
                if pos:
                    raw_exit = signal.exit_price or pos["entry_price"]
                    qty = signal.qty if signal.qty is not None else pos["qty"]
                    fill_price = await fill_service.resolve(
                        pos.get("exchange", "binance"), pos["symbol"], pos["side"], qty,
                        ref_price=raw_exit, is_close=True,
                    )
        except Exception as exc:
            logger.warning("Fill resolve failed for %s: %s", signal_id, exc)
            fill_price = None  # executor falls back to fixed-pct

    async with db.transaction():
        await db.log_signal(signal_id=signal_id, alpha_id=alpha_id,
                            signal_type=signal_type, payload=json.dumps(data))
        try:
            if signal.type == SignalType.OPEN:
                result = await executor.process_open(signal, fill_price=fill_price)
            elif signal.type == SignalType.MODIFY:
                result = await executor.process_modify(signal)
            elif signal.type == SignalType.CLOSE:
                result = await executor.process_close(signal, fill_price=fill_price)
            elif signal.type == SignalType.REGISTER_COLUMNS:
                result = await executor.process_register_columns(signal)
            else:
                result = None
            await db.mark_signal_processed(signal_id)
            return result
        except Exception as exc:
            logger.error("Error processing signal %s: %s", signal_id, exc)
            await db.mark_signal_processed(signal_id, error=str(exc))
            return None
```

- [ ] **Step 5: Run tests to verify they pass (new + existing main tests)**

Run: `cd worker && python -m pytest tests/test_main_fill_wiring.py tests/test_main.py -v`
Expected: PASS (new 2 + existing main tests)

- [ ] **Step 6: Wire the new tasks into `run_consumer`**

In `worker/app/main.py`, inside `run_consumer`, after `executor = Executor(...)` and
`cache = TickerPriceCache()`, add the order-book components:

```python
    ob_cache = ObExecCache()
    fill_service = None
    if settings.ENABLE_ORDERBOOK_SLIPPAGE:
        fill_service = FillService(
            SlippageClient(redis_client),
            slippage_pct=settings.SLIPPAGE_PCT,
            timeout=settings.SLIPPAGE_RPC_TIMEOUT,
        )
```

> Note: `redis_client` is created a few lines below in the current code (`redis_client =
> await connect_redis()`). Move the `fill_service` construction to **after** that line.

Add task creation alongside the existing `ticker_task` / `price_check_task` block. Where the
code does `if settings.ENABLE_WORKER_TPSL_AUTO_CLOSE:` set up, replace the price-check task so
it passes the side-aware provider and resolver, and add the ob_exec + sync tasks:

```python
        ob_exec_task = None
        ob_sync_task = None
        if settings.ENABLE_ORDERBOOK_SLIPPAGE:
            ob_exec_task = asyncio.create_task(
                run_ob_exec_subscriber(ob_cache, connect_redis, settings.ORDERBOOK_EXCHANGE)
            )
            ob_sync_task = asyncio.create_task(
                run_orderbook_sync_loop(db, redis_client, settings.CONSUMER_NAME,
                                        settings.ORDERBOOK_EXCHANGE, settings.ORDERBOOK_SYNC_INTERVAL)
            )

        if settings.ENABLE_WORKER_TPSL_AUTO_CLOSE:
            ticker_task = asyncio.create_task(run_ticker_subscriber(cache))
            price_check_task = asyncio.create_task(
                run_price_check_loop(db, executor, cache, ob_cache, fill_service)
            )
```

Pass the new args through `run_price_check_loop`:

```python
async def run_price_check_loop(db: Database, executor: Executor, cache: TickerPriceCache,
                               ob_cache: ObExecCache, fill_service) -> None:
    exit_price_fn = make_exit_price_fn(ob_cache, cache)
    fill_resolver = fill_service.resolve if fill_service is not None else None
    while True:
        try:
            await asyncio.sleep(settings.PRICE_CHECK_INTERVAL)
            symbols = await db.get_symbols_with_open_positions()
            if not symbols:
                continue
            hits = await executor.check_tpsl_hits(exit_price_fn, fill_resolver=fill_resolver)
            for hit in hits:
                logger.info("[TPSL] Auto-closed: %s", hit)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Price check error: %s", exc, exc_info=True)
            await asyncio.sleep(5)
```

Add the new tasks to the shutdown cleanup list at the end of `run_consumer`:

```python
        tasks = [task for task in (ticker_task, price_check_task, health_task,
                                   ob_exec_task, ob_sync_task) if task is not None]
```

(Declare `ob_exec_task = None` and `ob_sync_task = None` near the other `… = None`
declarations so they are defined even when disabled.)

Also pass `fill_service` into the signal loop call:

```python
                    result = await process_signal_message(data, db, executor, fill_service=fill_service)
```

- [ ] **Step 7: Event-driven subscribe on position open**

In `worker/app/main.py`, right after a successful OPEN result inside the signal loop (where
`logger.info("Processed %s signal: %s", …)` runs), publish an immediate subscribe so MDS
opens the depth book without waiting for the 5s poll:

```python
                    if result is not None and data.get("type") == "OPEN" and settings.ENABLE_ORDERBOOK_SLIPPAGE:
                        try:
                            await publish_subscribe(redis_client, settings.ORDERBOOK_EXCHANGE,
                                                    settings.CONSUMER_NAME, data.get("symbol", ""))
                        except Exception as exc:
                            logger.warning("orderbook subscribe publish failed: %s", exc)
```

- [ ] **Step 8: Run the full worker suite + import smoke check**

Run: `cd worker && python -m pytest tests/ -v`
Expected: PASS (all existing + new tests)

Run: `cd worker && python -c "import app.main; print('imports ok')"`
Expected: `imports ok`

- [ ] **Step 9: Commit**

```bash
git add worker/app/config.py worker/app/main.py worker/tests/test_main_fill_wiring.py
git commit -m "feat(paper-trade): wire ob_exec + slippage RPC into worker (b1)"
```

---

### Task 8: End-to-end verification note

**Files:** none (verification only)

- [ ] **Step 1: Run both test suites green**

Run: `cd worker && python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 2: Manual end-to-end smoke (requires MDS from the MDS plan running)**

With MDS (order-book subsystem enabled) and Redis up:
1. Start the worker with `ENABLE_ORDERBOOK_SLIPPAGE=True` and `ENABLE_WORKER_TPSL_AUTO_CLOSE=True`.
2. Send an OPEN signal for `BTCUSDT`; confirm the worker logs a `fill_price` that differs from
   the pure fixed-pct value (book-walked), and that MDS logs an `orderbook:subscribe` for it.
3. Confirm MDS publishes `ob_exec:binance:BTCUSDT` and the worker's TP/SL check uses best
   bid/ask (book_state READY).
4. Kill MDS; confirm the worker keeps triggering TP/SL on ticker and fills fall back to
   fixed-pct (no errors, `fill_price` reverts to the fixed-pct value).

- [ ] **Step 3: Commit (if any doc/notes added)**

```bash
git add -A
git commit -m "test(paper-trade): order-book integration verification notes" || echo "nothing to commit"
```

---

## Self-Review Notes (already applied)

- **Spec coverage (§8):** keep ticker baseline + layer ob_exec (§8.1 → T5/T7), side-aware
  trigger + RPC fill (§8.2 → T4/T2), RPC outside transaction (§8.2 → T7), fixed-pct fallback
  preserved (§8.2/§11 → T1/T3/T4), config keeps `SLIPPAGE_PCT` (§8.3 → T7), event-driven
  subscribe to shrink warmup gap (§8.1 → T7).
- **Backward compatibility:** every executor/main signature change is additive with defaults
  reproducing today's behavior; existing `test_executor.py` / `test_main.py` keep passing
  (verified in T1/T3/T4/T7 steps).
- **Type consistency:** `FillService.resolve(exchange, symbol, position_side, qty, ref_price,
  is_close)`, `order_side_for(position_side, is_close)`, `resolve_fill_price(resp, ref_price,
  position_side, is_close, slippage_pct)`, `check_tpsl_hits(price_source, fill_resolver)`,
  `process_open/close(signal, fill_price)`, `ObExecCache.update/side_price`,
  `make_exit_price_fn(ob_cache, ticker_cache)` are used consistently across tasks.
- **Assumptions flagged:** `get_positions_with_tpsl()` row keys (incl. optional `exchange`);
  exact placement of `redis_client`/task declarations in `run_consumer` (T7 notes).
- **Test dep:** `fakeredis` (with `fakeredis.aioredis`) is test-only (installed in T2 Step 1).
```
