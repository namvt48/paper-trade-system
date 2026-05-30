# Custom Trade History Columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow each alpha to register custom column definitions that appear in the trade history table, using the existing Redis signal infrastructure.

**Architecture:** Alpha sends a `REGISTER_COLUMNS` signal via Redis. Worker persists column definitions in a new `alpha_columns` DB table. Frontend reads column specs from DB and renders dynamic columns in the trade table by parsing the trade's `metadata` JSON.

**Tech Stack:** Python (worker/alpha), TypeScript/React (Next.js frontend), SQLite (via aiosqlite/better-sqlite3)

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `worker/app/models.py` | Add `REGISTER_COLUMNS` signal type and dataclass |
| Modify | `worker/app/db.py` | Add `alpha_columns` table, `register_alpha_columns()`, `get_alpha_columns()` |
| Modify | `worker/app/executor.py` | Add handler for `REGISTER_COLUMNS` signal |
| Modify | `worker/app/main.py` | Route `REGISTER_COLUMNS` signals to executor |
| Modify | `worker/tests/test_models.py` | Test parsing `REGISTER_COLUMNS` signal |
| Modify | `worker/tests/test_db.py` | Test `register_alpha_columns()` and `get_alpha_columns()` |
| Modify | `worker/tests/test_executor.py` | Test executor handles `REGISTER_COLUMNS` |
| Modify | `web/src/lib/types.ts` | Add `ColumnSpec` interface |
| Modify | `web/src/lib/db.ts` | Add `getAlphaColumns()` function |
| Modify | `web/src/components/trade-table.tsx` | Render dynamic columns + CSV export |
| Modify | `web/src/app/alpha/[id]/page.tsx` | Pass `columnSpecs` to `TradeTable` |
| Modify | `alphas/base/engine.py` | Add `register_columns()` helper |
| Modify | `alphas/alpha-1-fixed/app/engine.py` | Register columns on startup |
| Modify | `alphas/adx-trend-follow/app/engine.py` | Register columns on startup |
| Modify | `alphas/wilder/app/engine.py` | Register columns on startup |

---

### Task 1: Worker models — Add REGISTER_COLUMNS signal type

**Files:**
- Modify: `worker/app/models.py`

- [ ] **Step 1: Add `REGISTER_COLUMNS` to `SignalType` enum and add `RegisterColumnsSignal` dataclass**

In `worker/app/models.py`, add `REGISTER_COLUMNS = "REGISTER_COLUMNS"` to the `SignalType` enum (after line 9), add the `RegisterColumnsSignal` dataclass, and update `parse_signal()` to handle the new type.

```python
class SignalType(str, Enum):
    OPEN = "OPEN"
    MODIFY = "MODIFY"
    CLOSE = "CLOSE"
    REGISTER_COLUMNS = "REGISTER_COLUMNS"
```

Add after `CloseSignal` dataclass (after line 52):

```python
@dataclass
class RegisterColumnsSignal:
    type: SignalType
    alpha_id: str
    signal_id: str
    columns: str
```

Add at the end of `parse_signal()` (before the final closing of the function, after the `elif st == SignalType.CLOSE` block):

```python
    elif st == SignalType.REGISTER_COLUMNS:
        return RegisterColumnsSignal(
            type=st,
            alpha_id=data["alpha_id"],
            signal_id=data.get("signal_id", ""),
            columns=data.get("columns", "[]"),
        )
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `cd worker && python -m pytest tests/test_models.py -v`
Expected: All existing tests pass

- [ ] **Step 3: Commit**

```bash
git add worker/app/models.py
git commit -m "feat(worker): add REGISTER_COLUMNS signal type and dataclass"
```

---

### Task 2: Worker models — Test REGISTER_COLUMNS parsing

**Files:**
- Modify: `worker/tests/test_models.py`

- [ ] **Step 1: Write test for parsing REGISTER_COLUMNS signal**

In `worker/tests/test_models.py`, add at the end:

```python
def test_parse_register_columns_signal():
    data = {
        "type": "REGISTER_COLUMNS",
        "alpha_id": "alpha-1-fixed",
        "signal_id": "sig-reg-001",
        "columns": '[{"key": "atr", "label": "ATR", "type": "number", "decimals": 6}]',
    }
    signal = parse_signal(data)
    assert isinstance(signal, RegisterColumnsSignal)
    assert signal.alpha_id == "alpha-1-fixed"
    assert signal.columns == '[{"key": "atr", "label": "ATR", "type": "number", "decimals": 6}]'
```

Also add `RegisterColumnsSignal` to the import on line 2:

```python
from app.models import parse_signal, OpenSignal, ModifySignal, CloseSignal, SignalType, RegisterColumnsSignal
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd worker && python -m pytest tests/test_models.py::test_parse_register_columns_signal -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add worker/tests/test_models.py
git commit -m "test(worker): add REGISTER_COLUMNS signal parsing test"
```

---

### Task 3: Worker DB — Add alpha_columns table and methods

**Files:**
- Modify: `worker/app/db.py`

- [ ] **Step 1: Add `alpha_columns` table creation in `_create_tables`**

In `worker/app/db.py`, add after the `signals` table creation (after the `CREATE INDEX` statements, before the closing `""")` on line 86):

```sql
            CREATE TABLE IF NOT EXISTS alpha_columns (
                alpha_id TEXT NOT NULL,
                column_key TEXT NOT NULL,
                label TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'text',
                decimals INTEGER DEFAULT 0,
                sort_order INTEGER DEFAULT 0,
                PRIMARY KEY (alpha_id, column_key)
            );
```

- [ ] **Step 2: Add `register_alpha_columns` and `get_alpha_columns` methods**

Add these methods at the end of the `Database` class (after `get_positions_with_tpsl`):

```python
    async def register_alpha_columns(self, alpha_id: str, columns: list[dict]) -> None:
        await self._conn.execute(
            "DELETE FROM alpha_columns WHERE alpha_id = ?", (alpha_id,)
        )
        for i, col in enumerate(columns):
            await self._conn.execute(
                """INSERT INTO alpha_columns (alpha_id, column_key, label, type, decimals, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    alpha_id,
                    col["key"],
                    col.get("label", col["key"]),
                    col.get("type", "text"),
                    col.get("decimals", 0),
                    i,
                ),
            )
        await self._conn.commit()

    async def get_alpha_columns(self, alpha_id: str) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM alpha_columns WHERE alpha_id = ? ORDER BY sort_order",
            (alpha_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 3: Run existing DB tests to verify no regressions**

Run: `cd worker && python -m pytest tests/test_db.py -v`
Expected: All existing tests pass

- [ ] **Step 4: Commit**

```bash
git add worker/app/db.py
git commit -m "feat(worker): add alpha_columns table and DB methods"
```

---

### Task 4: Worker DB — Test alpha_columns methods

**Files:**
- Modify: `worker/tests/test_db.py`

- [ ] **Step 1: Write tests for `register_alpha_columns` and `get_alpha_columns`**

In `worker/tests/test_db.py`, add at the end:

```python
@pytest.mark.asyncio
async def test_register_alpha_columns(db):
    await db.register_alpha("test-alpha")
    columns = [
        {"key": "atr", "label": "ATR", "type": "number", "decimals": 6},
        {"key": "trend", "label": "Trend", "type": "text"},
    ]
    await db.register_alpha_columns("test-alpha", columns)
    result = await db.get_alpha_columns("test-alpha")
    assert len(result) == 2
    assert result[0]["column_key"] == "atr"
    assert result[0]["label"] == "ATR"
    assert result[0]["type"] == "number"
    assert result[0]["decimals"] == 6
    assert result[0]["sort_order"] == 0
    assert result[1]["column_key"] == "trend"
    assert result[1]["sort_order"] == 1


@pytest.mark.asyncio
async def test_register_alpha_columns_replaces_existing(db):
    await db.register_alpha("test-alpha")
    await db.register_alpha_columns("test-alpha", [
        {"key": "atr", "label": "ATR", "type": "number", "decimals": 6},
    ])
    await db.register_alpha_columns("test-alpha", [
        {"key": "vol_spike", "label": "Vol Spike", "type": "number", "decimals": 2},
        {"key": "btc_adx", "label": "BTC ADX", "type": "number", "decimals": 2},
    ])
    result = await db.get_alpha_columns("test-alpha")
    assert len(result) == 2
    assert result[0]["column_key"] == "vol_spike"
    assert result[1]["column_key"] == "btc_adx"


@pytest.mark.asyncio
async def test_get_alpha_columns_empty(db):
    result = await db.get_alpha_columns("nonexistent-alpha")
    assert result == []
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd worker && python -m pytest tests/test_db.py::test_register_alpha_columns tests/test_db.py::test_register_alpha_columns_replaces_existing tests/test_db.py::test_get_alpha_columns_empty -v`
Expected: All 3 PASS

- [ ] **Step 3: Commit**

```bash
git add worker/tests/test_db.py
git commit -m "test(worker): add alpha_columns DB tests"
```

---

### Task 5: Worker executor — Handle REGISTER_COLUMNS signal

**Files:**
- Modify: `worker/app/executor.py`

- [ ] **Step 1: Add `process_register_columns` method to `Executor`**

In `worker/app/executor.py`, update the import on line 4 to include `RegisterColumnsSignal`:

```python
from app.models import OpenSignal, ModifySignal, CloseSignal, RegisterColumnsSignal, SignalType
```

Add method to the `Executor` class (after `process_close`):

```python
    async def process_register_columns(self, signal: RegisterColumnsSignal) -> dict:
        import json
        try:
            columns = json.loads(signal.columns)
        except (json.JSONDecodeError, TypeError):
            raise ValueError(f"Invalid columns JSON: {signal.columns}")

        if not isinstance(columns, list):
            raise ValueError("columns must be a JSON array")

        await self.db.register_alpha(signal.alpha_id)
        await self.db.register_alpha_columns(signal.alpha_id, columns)
        return {"alpha_id": signal.alpha_id, "columns_registered": len(columns)}
```

- [ ] **Step 2: Commit**

```bash
git add worker/app/executor.py
git commit -m "feat(worker): add REGISTER_COLUMNS handler in executor"
```

---

### Task 6: Worker main — Route REGISTER_COLUMNS signals

**Files:**
- Modify: `worker/app/main.py`

- [ ] **Step 1: Add routing for `REGISTER_COLUMNS` in `process_signal_message`**

In `worker/app/main.py`, update the import on line 11 to include `RegisterColumnsSignal`:

```python
from app.models import SignalType, parse_signal, RegisterColumnsSignal
```

In the `process_signal_message` function, after the `elif signal.type == SignalType.CLOSE` block (after line 68), add:

```python
        elif signal.type == SignalType.REGISTER_COLUMNS:
            result = await executor.process_register_columns(signal)
```

- [ ] **Step 2: Run all worker tests to verify no regressions**

Run: `cd worker && python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add worker/app/main.py
git commit -m "feat(worker): route REGISTER_COLUMNS signals to executor"
```

---

### Task 7: Worker executor — Test REGISTER_COLUMNS handling

**Files:**
- Modify: `worker/tests/test_executor.py`

- [ ] **Step 1: Write test for `process_register_columns`**

In `worker/tests/test_executor.py`, update the import on line 4 to include `RegisterColumnsSignal`:

```python
from app.models import OpenSignal, ModifySignal, CloseSignal, RegisterColumnsSignal, SignalType
```

Add at the end of the file:

```python
@pytest.mark.asyncio
async def test_process_register_columns(executor):
    signal = RegisterColumnsSignal(
        type=SignalType.REGISTER_COLUMNS,
        alpha_id="test-alpha",
        signal_id="sig-reg-001",
        columns='[{"key": "atr", "label": "ATR", "type": "number", "decimals": 6}]',
    )
    result = await executor.process_register_columns(signal)
    assert result["columns_registered"] == 1
    cols = await executor.db.get_alpha_columns("test-alpha")
    assert len(cols) == 1
    assert cols[0]["column_key"] == "atr"


@pytest.mark.asyncio
async def test_process_register_columns_invalid_json(executor):
    signal = RegisterColumnsSignal(
        type=SignalType.REGISTER_COLUMNS,
        alpha_id="test-alpha",
        signal_id="sig-reg-002",
        columns="not-json",
    )
    with pytest.raises(ValueError, match="Invalid columns JSON"):
        await executor.process_register_columns(signal)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd worker && python -m pytest tests/test_executor.py::test_process_register_columns tests/test_executor.py::test_process_register_columns_invalid_json -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add worker/tests/test_executor.py
git commit -m "test(worker): add executor REGISTER_COLUMNS tests"
```

---

### Task 8: Frontend types — Add ColumnSpec interface

**Files:**
- Modify: `web/src/lib/types.ts`

- [ ] **Step 1: Add `ColumnSpec` interface**

In `web/src/lib/types.ts`, add after the `AlphaStats` interface (after line 64):

```typescript
export interface ColumnSpec {
  key: string;
  label: string;
  type: "number" | "text";
  decimals?: number;
  sort_order: number;
}
```

- [ ] **Step 2: Commit**

```bash
git add web/src/lib/types.ts
git commit -m "feat(web): add ColumnSpec type"
```

---

### Task 9: Frontend DB — Add getAlphaColumns function

**Files:**
- Modify: `web/src/lib/db.ts`

- [ ] **Step 1: Add `getAlphaColumns` function**

In `web/src/lib/db.ts`, add `ColumnSpec` to the import on line 4:

```typescript
import type { Alpha, Position, Trade, EquityPoint, AlphaStats, AlphaConfig, ColumnSpec } from "./types";
```

Add function at the end of the file (after `getDashboardData`):

```typescript
export function getAlphaColumns(alphaId: string): ColumnSpec[] {
  const db = tryGetDb();
  if (!db) return [];
  try {
    return db.prepare("SELECT * FROM alpha_columns WHERE alpha_id = ? ORDER BY sort_order").all(alphaId) as ColumnSpec[];
  } finally {
    db.close();
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add web/src/lib/db.ts
git commit -m "feat(web): add getAlphaColumns DB function"
```

---

### Task 10: Frontend API — Add column specs endpoint

**Files:**
- Create: `web/src/app/api/columns/route.ts`

- [ ] **Step 1: Create columns API route**

Create `web/src/app/api/columns/route.ts`:

```typescript
import { NextResponse } from "next/server";
import { getAlphaColumns } from "@/lib/db";
import { withRoute } from "@/lib/api";

export const GET = withRoute("api/columns", async (request) => {
  const { searchParams } = new URL(request.url);
  const alphaId = searchParams.get("alpha_id");

  if (!alphaId) {
    return NextResponse.json({ error: "alpha_id required" }, { status: 400 });
  }

  return NextResponse.json(getAlphaColumns(alphaId));
});
```

- [ ] **Step 2: Commit**

```bash
git add web/src/app/api/columns/route.ts
git commit -m "feat(web): add /api/columns endpoint"
```

---

### Task 11: Frontend TradeTable — Render dynamic columns + CSV export

**Files:**
- Modify: `web/src/components/trade-table.tsx`

- [ ] **Step 1: Update TradeTable to accept and render `columnSpecs`**

Update the import on line 4:

```typescript
import type { Trade, ColumnSpec } from "@/lib/types";
```

Update the `TradeTableProps` interface (line 8-10):

```typescript
interface TradeTableProps {
  trades: Trade[];
  columnSpecs?: ColumnSpec[];
}
```

Update the `downloadCsv` function (lines 100-128) to include custom columns. Replace the entire function:

```typescript
function downloadCsv(trades: Trade[], columnSpecs?: ColumnSpec[]) {
  const customHeaders = (columnSpecs ?? []).map((c) => c.label);
  const customKeys = (columnSpecs ?? []).map((c) => c.key);
  const headers = [
    "trade_id", "symbol", "side", "entry_price", "exit_price", "qty",
    "leverage", "tp", "sl", "pnl", "pnl_percent", "fee",
    ...customHeaders,
    "reason", "duration_hours", "opened_at", "closed_at",
  ];
  const escape = (v: unknown) => {
    const s = v == null ? "" : String(v);
    return s.includes(",") || s.includes('"') || s.includes("\n")
      ? `"${s.replace(/"/g, '""')}"`
      : s;
  };
  const rows = trades.map((t) => {
    let meta: Record<string, unknown> = {};
    try { meta = JSON.parse(t.metadata || "{}"); } catch {}
    const customValues = customKeys.map((key) => {
      const val = meta[key];
      if (val == null) return "";
      return String(val);
    });
    return [
      t.trade_id, t.symbol, t.side, t.entry_price, t.exit_price, t.qty,
      t.leverage, t.tp ?? "", t.sl ?? "", t.pnl, t.pnl_percent,
      (t as any).fee ?? "",
      ...customValues,
      t.reason, t.duration_hours, t.opened_at, t.closed_at,
    ].map(escape).join(",");
  });
  const csv = [headers.join(","), ...rows].join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `trades-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
```

Update the component signature (line 130):

```typescript
export function TradeTable({ trades, columnSpecs }: TradeTableProps) {
```

Update the download CSV button `onClick` (line 143):

```typescript
          onClick={() => downloadCsv(trades, columnSpecs)}
```

Add custom column headers in `<thead>` after the "Fee" header `<th>` (after line 166) and before the "Reason" header `<th>`:

```typescript
              {(columnSpecs ?? []).map((spec) => (
                <th key={spec.key} className="text-right py-3 px-3 whitespace-nowrap">{spec.label}</th>
              ))}
```

Add custom column cells in `<tbody>` after the fee `<td>` (after line 202) and before the reason `<td>`. First, add metadata parsing at the top of the row render. Replace the visible.map block (lines 174-208) with:

```typescript
            {visible.map((t, i) => {
              let meta: Record<string, unknown> = {};
              try { meta = JSON.parse(t.metadata || "{}"); } catch {}
              return (
              <tr
                key={t.trade_id}
                className={`border-b border-slate-700/40 hover:bg-slate-700/30 transition-colors ${i % 2 === 0 ? "" : "bg-slate-800/30"}`}
              >
                <td className="py-2 px-3 font-mono text-slate-200 whitespace-nowrap">{t.symbol}</td>
                <td className={`py-2 px-3 font-semibold text-xs whitespace-nowrap ${t.side === "LONG" ? "text-emerald-400" : "text-rose-400"}`}>{t.side}</td>
                <td className="py-2 px-3 text-right font-mono text-slate-300 whitespace-nowrap">{fmtPrice(t.entry_price)}</td>
                <td className="py-2 px-3 text-right font-mono text-slate-300 whitespace-nowrap">{fmtPrice(t.exit_price)}</td>
                {(() => {
                  const dir = t.side === "LONG" ? 1 : -1;
                  const movePct = dir * (t.exit_price - t.entry_price) / t.entry_price * 100;
                  return (
                    <td className={`py-2 px-3 text-right font-mono text-xs whitespace-nowrap ${movePct >= 0 ? "text-emerald-400/80" : "text-rose-400/80"}`}>
                      {movePct >= 0 ? "+" : ""}{movePct.toFixed(3)}%
                    </td>
                  );
                })()}
                <td className="py-2 px-3 text-right font-mono text-slate-400 whitespace-nowrap text-xs">{parseFloat(t.qty.toPrecision(6)).toString()}</td>
                <td className="py-2 px-3 text-right font-mono text-emerald-400/80 whitespace-nowrap">{fmtPrice(t.tp)}</td>
                <td className="py-2 px-3 text-right font-mono text-rose-400/80 whitespace-nowrap">{fmtPrice(t.sl)}</td>
                <td className={`py-2 px-3 text-right font-mono font-semibold whitespace-nowrap ${t.pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  {t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(4)}
                </td>
                <td className={`py-2 px-3 text-right font-mono whitespace-nowrap ${t.pnl_percent >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  {t.pnl_percent >= 0 ? "+" : ""}{t.pnl_percent.toFixed(2)}%
                </td>
                <td className="py-2 px-3 text-right font-mono text-slate-500 text-xs whitespace-nowrap">
                  {(t as any).fee != null ? (t as any).fee.toFixed(4) : "—"}
                </td>
                {(columnSpecs ?? []).map((spec) => {
                  const val = meta[spec.key];
                  if (val == null) return <td key={spec.key} className="py-2 px-3 text-right font-mono text-slate-500 text-xs whitespace-nowrap">—</td>;
                  if (spec.type === "number" && typeof val === "number") {
                    return <td key={spec.key} className="py-2 px-3 text-right font-mono text-slate-300 text-xs whitespace-nowrap">{val.toFixed(spec.decimals ?? 0)}</td>;
                  }
                  return <td key={spec.key} className="py-2 px-3 text-right font-mono text-slate-300 text-xs whitespace-nowrap">{String(val)}</td>;
                })}
                <td className="py-2 px-3 text-slate-400 text-xs whitespace-nowrap">{t.reason}</td>
                <td className="py-2 px-3 text-right font-mono text-slate-400 text-xs whitespace-nowrap">{fmtDuration(t.duration_hours)}</td>
                <td className="py-2 px-3 text-slate-400 font-mono text-xs whitespace-nowrap">{fmtDate(t.opened_at)}</td>
                <td className="py-2 px-3 text-slate-400 font-mono text-xs whitespace-nowrap">{fmtDate(t.closed_at)}</td>
              </tr>
              );
            })}
```

- [ ] **Step 2: Verify the frontend builds**

Run: `cd web && npx next build 2>&1 | tail -20`
Expected: Build succeeds with no TypeScript errors

- [ ] **Step 3: Commit**

```bash
git add web/src/components/trade-table.tsx
git commit -m "feat(web): render dynamic custom columns in trade table with CSV export"
```

---

### Task 12: Frontend alpha detail page — Pass columnSpecs to TradeTable

**Files:**
- Modify: `web/src/app/alpha/[id]/page.tsx`

- [ ] **Step 1: Import `getAlphaColumns` and pass to `TradeTable`**

In `web/src/app/alpha/[id]/page.tsx`, update the import on line 1 to include `getAlphaColumns`:

```typescript
import { getAlpha, getAlphaStats, getTrades, getEquityCurve, getOpenPositions, getAlphaConfig, getAlphaColumns } from "@/lib/db";
```

Add `columnSpecs` fetch alongside other data (after line 37, after `const config = getAlphaConfig(id);`):

```typescript
  const columnSpecs = getAlphaColumns(id);
```

Update the `TradeTable` usage (line 105):

```typescript
        <TradeTable trades={trades} columnSpecs={columnSpecs} />
```

- [ ] **Step 2: Verify the frontend builds**

Run: `cd web && npx next build 2>&1 | tail -20`
Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
git add web/src/app/alpha/[id]/page.tsx
git commit -m "feat(web): pass columnSpecs to TradeTable in alpha detail page"
```

---

### Task 13: Base engine — Add register_columns helper

**Files:**
- Modify: `alphas/base/engine.py`

- [ ] **Step 1: Add `register_columns` method to `BaseEngine`**

In `alphas/base/engine.py`, add `json` to the imports (line 3 already has it). Add method to the `BaseEngine` class (after `push_signal` on line 346):

```python
    def register_columns(self, columns: list[dict]) -> None:
        self.push_signal("REGISTER_COLUMNS", columns=json.dumps(columns))
```

- [ ] **Step 2: Commit**

```bash
git add alphas/base/engine.py
git commit -m "feat(base): add register_columns helper to BaseEngine"
```

---

### Task 14: Alpha-1-fixed — Register custom columns

**Files:**
- Modify: `alphas/alpha-1-fixed/app/engine.py`

- [ ] **Step 1: Add `register_columns` call in `on_warmup_complete`**

In `alphas/alpha-1-fixed/app/engine.py`, add at the end of `on_warmup_complete` (after the `logger.info` call on line 148):

```python
        self.register_columns([
            {"key": "atr", "label": "ATR", "type": "number", "decimals": 6},
            {"key": "poc", "label": "POC", "type": "number", "decimals": 6},
            {"key": "trail_distance", "label": "Trail Dist", "type": "number", "decimals": 6},
            {"key": "trend", "label": "Trend", "type": "text"},
            {"key": "leverage", "label": "Lev", "type": "number", "decimals": 0},
            {"key": "margin", "label": "Margin", "type": "number", "decimals": 2},
        ])
```

- [ ] **Step 2: Commit**

```bash
git add alphas/alpha-1-fixed/app/engine.py
git commit -m "feat(alpha-1-fixed): register custom columns"
```

---

### Task 15: ADX-trend-follow — Register custom columns

**Files:**
- Modify: `alphas/adx-trend-follow/app/engine.py`

- [ ] **Step 1: Add `register_columns` call in `__init__`**

In `alphas/adx-trend-follow/app/engine.py`, add after `self._open_positions: dict[str, dict] = {}` (after line 23):

```python
        self.register_columns([
            {"key": "vol_spike", "label": "Vol Spike", "type": "number", "decimals": 2},
            {"key": "price_move", "label": "Price Move", "type": "number", "decimals": 6},
            {"key": "btc_adx", "label": "BTC ADX", "type": "number", "decimals": 2},
        ])
```

- [ ] **Step 2: Commit**

```bash
git add alphas/adx-trend-follow/app/engine.py
git commit -m "feat(adx-trend-follow): register custom columns"
```

---

### Task 16: Wilder — Register custom columns

**Files:**
- Modify: `alphas/wilder/app/engine.py`

- [ ] **Step 1: Add `register_columns` call in `__init__`**

In `alphas/wilder/app/engine.py`, add after `self._open_positions: dict[str, dict] = {}` (after line 27):

```python
        self.register_columns([
            {"key": "regime", "label": "Regime", "type": "text"},
            {"key": "adx", "label": "ADX", "type": "number", "decimals": 2},
            {"key": "plus_di", "label": "+DI", "type": "number", "decimals": 2},
            {"key": "minus_di", "label": "-DI", "type": "number", "decimals": 2},
            {"key": "rsi_curr", "label": "RSI", "type": "number", "decimals": 2},
            {"key": "atr", "label": "ATR", "type": "number", "decimals": 6},
        ])
```

- [ ] **Step 2: Commit**

```bash
git add alphas/wilder/app/engine.py
git commit -m "feat(wilder): register custom columns"
```

---

### Task 17: Final verification

**Files:**
- All modified files

- [ ] **Step 1: Run all worker tests**

Run: `cd worker && python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: Verify frontend builds**

Run: `cd web && npx next build 2>&1 | tail -20`
Expected: Build succeeds with no errors

- [ ] **Step 3: Commit any remaining changes**

```bash
git add -A
git commit -m "chore: final cleanup for custom trade columns feature"
```
