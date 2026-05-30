# Custom Trade History Columns

## Problem

Each alpha stores alpha-specific data in the `metadata` JSON column of trades, but the trade history table only displays a fixed set of common columns. Alpha operators cannot see their custom indicators (ATR, POC, vol_spike, etc.) without querying the DB directly.

## Solution

Allow each alpha to register its own column definitions via the existing Redis signal infrastructure. The worker persists these definitions in a new `alpha_columns` DB table. The frontend reads column specs alongside trades and renders dynamic columns in the trade table.

## Data Flow

```
Alpha (Python)                    Worker (Python)              Frontend (Next.js)
    |                                  |                            |
    |-- push_signal(                   |                            |
    |     "REGISTER_COLUMNS",          |                            |
    |     columns=[...] -------------> |                            |
    |   )                              |                            |
    |                                  |-- Parse signal              |
    |                                  |-- Upsert alpha_columns      |
    |                                  |   in SQLite                 |
    |                                  |                            |
    |  push_signal("OPEN",             |                            |
    |    metadata={...}) ------------> |                            |
    |                                  |-- Store metadata as usual   |
    |                                  |                            |
    |                                  |    GET /api/alphas/[id] <---|
    |                                  |-- Return trades + --------->|
    |                                  |   column_specs              |
    |                                  |                            |
    |                                  |                            |-- Parse metadata JSON
    |                                  |                            |-- Render dynamic columns
    |                                  |                            |   in TradeTable
```

## Signal Format

Alpha sends a `REGISTER_COLUMNS` signal at startup:

```python
self.push_signal(
    "REGISTER_COLUMNS",
    columns=json.dumps([
        {"key": "atr", "label": "ATR", "type": "number", "decimals": 6},
        {"key": "poc", "label": "POC", "type": "number", "decimals": 6},
        {"key": "trail_distance", "label": "Trail Dist", "type": "number", "decimals": 6},
        {"key": "trend", "label": "Trend", "type": "text"},
        {"key": "leverage", "label": "Lev", "type": "number", "decimals": 0},
        {"key": "margin", "label": "Margin", "type": "number", "decimals": 2},
    ]),
)
```

### Column Definition Schema

| Field      | Type    | Required | Description                                      |
|------------|---------|----------|--------------------------------------------------|
| `key`      | string  | yes      | Must match a key in the trade's metadata JSON    |
| `label`    | string  | yes      | Column header displayed in the trade table       |
| `type`     | string  | yes      | `"number"` or `"text"`                           |
| `decimals` | integer | no       | Decimal places for `number` type (default: 0)    |

- `number`: formatted with the specified decimal places
- `text`: displayed as raw string

## DB Schema (Worker)

New table `alpha_columns`:

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

The worker handles `REGISTER_COLUMNS` by:
1. Deleting all existing columns for the given `alpha_id`
2. Inserting the new column definitions with `sort_order` matching their array index

This replace-all strategy keeps the implementation simple and ensures stale columns are removed.

## Worker Changes

### models.py

Add `REGISTER_COLUMNS` to `SignalType` enum:

```python
class SignalType(str, Enum):
    OPEN = "OPEN"
    MODIFY = "MODIFY"
    CLOSE = "CLOSE"
    REGISTER_COLUMNS = "REGISTER_COLUMNS"
```

Add `RegisterColumnsSignal` dataclass:

```python
@dataclass
class RegisterColumnsSignal:
    type: SignalType
    alpha_id: str
    columns: str  # JSON string of column definitions
```

Update `parse_signal` to handle the new type.

### executor.py

Add handler for `REGISTER_COLUMNS` that:
1. Parses the `columns` JSON
2. Calls `db.register_alpha_columns(alpha_id, columns_list)`

### db.py

Add methods:

- `register_alpha_columns(alpha_id: str, columns: list[dict])` — delete existing + insert new
- `get_alpha_columns(alpha_id: str) -> list[dict]` — return column specs ordered by `sort_order`

## API Changes

The existing `GET /api/alphas/[id]` endpoint (or a new dedicated endpoint) returns `column_specs` alongside trades:

```json
{
  "trades": [...],
  "column_specs": [
    {"key": "atr", "label": "ATR", "type": "number", "decimals": 6, "sort_order": 0}
  ]
}
```

The `db.ts` module adds a function:

```typescript
export function getAlphaColumns(alphaId: string): ColumnSpec[]
```

## Frontend Changes

### types.ts

Add `ColumnSpec` interface:

```typescript
export interface ColumnSpec {
  key: string;
  label: string;
  type: "number" | "text";
  decimals?: number;
  sort_order: number;
}
```

### TradeTable

Accept new prop `columnSpecs: ColumnSpec[]`:

- Parse each trade's `metadata` JSON string into a `Record<string, unknown>`
- Render custom columns after the fixed common columns, before the "Reason" column
- For `number` type: format with `decimals` decimal places using `toFixed()`
- For `text` type: display as-is
- CSV download includes custom column headers and values

### alpha/[id]/page.tsx

- Fetch `columnSpecs` from DB
- Pass to `TradeTable` component

## Base Engine Helper

Add convenience method to `BaseEngine`:

```python
def register_columns(self, columns: list[dict]) -> None:
    self.push_signal("REGISTER_COLUMNS", columns=json.dumps(columns))
```

Alphas call this once in `__init__` or `on_warmup_complete`.

## Example Usage

### alpha-1-fixed

```python
def on_warmup_complete(self) -> None:
    # ... existing trend reconstruction ...
    self.register_columns([
        {"key": "atr", "label": "ATR", "type": "number", "decimals": 6},
        {"key": "poc", "label": "POC", "type": "number", "decimals": 6},
        {"key": "trail_distance", "label": "Trail Dist", "type": "number", "decimals": 6},
        {"key": "trend", "label": "Trend", "type": "text"},
        {"key": "leverage", "label": "Lev", "type": "number", "decimals": 0},
        {"key": "margin", "label": "Margin", "type": "number", "decimals": 2},
    ])
```

### adx-trend-follow

```python
def __init__(self):
    super().__init__(settings)
    self._open_positions: dict[str, dict] = {}
    self.register_columns([
        {"key": "vol_spike", "label": "Vol Spike", "type": "number", "decimals": 2},
        {"key": "price_move", "label": "Price Move", "type": "number", "decimals": 6},
        {"key": "btc_adx", "label": "BTC ADX", "type": "number", "decimals": 2},
    ])
```

## Scope

- Worker: new signal type, DB table, handler
- Frontend: dynamic columns in TradeTable, CSV export, API
- Alphas: call `register_columns()` at startup
- No changes to existing trade/metadata storage
