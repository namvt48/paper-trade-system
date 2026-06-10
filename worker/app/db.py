import aiosqlite
import json
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional


def merge_trade_metadata(open_metadata: str | None, close_metadata: str | None) -> str:
    """Merge open and close metadata keeping open keys at top level.

    Open metadata keys remain flat so registered column display still works.
    Close audit data is nested under 'close'.
    """
    result: dict = {}

    if open_metadata:
        try:
            parsed = json.loads(open_metadata)
            if isinstance(parsed, dict):
                result.update(parsed)
            else:
                result["open_raw"] = open_metadata
        except (json.JSONDecodeError, TypeError):
            result["open_raw"] = open_metadata

    if close_metadata:
        try:
            parsed = json.loads(close_metadata)
            if isinstance(parsed, dict):
                result["close"] = parsed
            else:
                result["close_raw"] = close_metadata
        except (json.JSONDecodeError, TypeError):
            result["close_raw"] = close_metadata

    return json.dumps(result)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._tx_depth = 0

    async def init(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA temp_store=MEMORY")
        await self._create_tables()
        await self._migrate()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _create_tables(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS alphas (
                alpha_id TEXT PRIMARY KEY,
                display_name TEXT,
                created_at TEXT,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                alpha_id TEXT REFERENCES alphas(alpha_id),
                signal_id TEXT,
                symbol TEXT,
                side TEXT,
                entry_price REAL,
                qty REAL,
                tp REAL,
                sl REAL,
                leverage INTEGER DEFAULT 1,
                opened_at TEXT,
                metadata TEXT,
                exchange TEXT DEFAULT 'binance',
                fee_pct REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                position_id TEXT,
                alpha_id TEXT REFERENCES alphas(alpha_id),
                signal_id TEXT,
                symbol TEXT,
                side TEXT,
                entry_price REAL,
                exit_price REAL,
                qty REAL,
                pnl REAL,
                pnl_percent REAL,
                leverage INTEGER,
                tp REAL,
                sl REAL,
                reason TEXT,
                duration_hours REAL,
                opened_at TEXT,
                closed_at TEXT,
                metadata TEXT,
                fee REAL DEFAULT 0.0,
                exchange TEXT DEFAULT 'binance'
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT,
                alpha_id TEXT,
                type TEXT,
                payload TEXT,
                received_at TEXT,
                processed INTEGER DEFAULT 0,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trades_alpha_closed ON trades(alpha_id, closed_at);
            CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
            CREATE INDEX IF NOT EXISTS idx_trades_pnl ON trades(pnl);
            CREATE INDEX IF NOT EXISTS idx_positions_alpha_symbol ON positions(alpha_id, symbol);
            CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
            CREATE INDEX IF NOT EXISTS idx_signals_alpha_received ON signals(alpha_id, received_at);
            CREATE INDEX IF NOT EXISTS idx_signals_signal_id ON signals(signal_id);

            CREATE TABLE IF NOT EXISTS alpha_columns (
                alpha_id TEXT NOT NULL,
                column_key TEXT NOT NULL,
                label TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'text',
                decimals INTEGER DEFAULT 0,
                sort_order INTEGER DEFAULT 0,
                PRIMARY KEY (alpha_id, column_key)
            );
        """)
        await self._conn.commit()

    async def _commit(self):
        if self._tx_depth == 0:
            await self._conn.commit()

    @asynccontextmanager
    async def transaction(self):
        outermost = self._tx_depth == 0
        self._tx_depth += 1
        try:
            if outermost:
                await self._conn.execute("BEGIN IMMEDIATE")
            yield
            if outermost:
                await self._conn.commit()
        except Exception:
            if outermost:
                await self._conn.rollback()
            raise
        finally:
            self._tx_depth -= 1

    async def _migrate(self):
        migrations = [
            "ALTER TABLE positions ADD COLUMN exchange TEXT DEFAULT 'binance'",
            "ALTER TABLE positions ADD COLUMN fee_pct REAL DEFAULT 0.0",
            "ALTER TABLE trades ADD COLUMN fee REAL DEFAULT 0.0",
            "ALTER TABLE trades ADD COLUMN exchange TEXT DEFAULT 'binance'",
            "CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at)",
            "CREATE INDEX IF NOT EXISTS idx_trades_pnl ON trades(pnl)",
            "CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_signals_signal_id ON signals(signal_id)",
        ]
        for sql in migrations:
            try:
                await self._conn.execute(sql)
            except Exception:
                pass  # column already exists
        await self._conn.commit()

    async def register_alpha(self, alpha_id: str, display_name: str = None):
        now = datetime.utcnow().isoformat()
        await self._conn.execute(
            "INSERT OR IGNORE INTO alphas (alpha_id, display_name, created_at, status) VALUES (?, ?, ?, 'active')",
            (alpha_id, display_name or alpha_id, now),
        )
        await self._commit()

    async def get_alpha(self, alpha_id: str):
        cursor = await self._conn.execute(
            "SELECT * FROM alphas WHERE alpha_id = ?", (alpha_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def create_position(
        self,
        position_id: str,
        alpha_id: str,
        signal_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        qty: float,
        opened_at: str,
        tp: float = None,
        sl: float = None,
        leverage: int = 1,
        metadata: str = "{}",
        exchange: str = "binance",
        fee_pct: float = 0.0,
    ):
        await self._conn.execute(
            """INSERT INTO positions
               (position_id, alpha_id, signal_id, symbol, side, entry_price, qty, tp, sl, leverage, opened_at, metadata, exchange, fee_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (position_id, alpha_id, signal_id, symbol, side, entry_price, qty, tp, sl, leverage, opened_at, metadata, exchange, fee_pct),
        )
        await self._commit()
        return position_id

    async def get_position(self, position_id: str):
        cursor = await self._conn.execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_open_position_by_alpha_symbol(self, alpha_id: str, symbol: str):
        cursor = await self._conn.execute(
            "SELECT * FROM positions WHERE alpha_id = ? AND symbol = ?",
            (alpha_id, symbol),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def modify_position(self, position_id: str, tp: float = None, sl: float = None):
        updates = []
        params = []
        if tp is not None:
            updates.append("tp = ?")
            params.append(tp)
        if sl is not None:
            updates.append("sl = ?")
            params.append(sl)
        if not updates:
            return
        params.append(position_id)
        await self._conn.execute(
            f"UPDATE positions SET {', '.join(updates)} WHERE position_id = ?",
            params,
        )
        await self._commit()

    async def close_position(
        self,
        position_id: str,
        exit_price: float,
        reason: str,
        closed_at: str,
        close_metadata: str | None = None,
        qty: float | None = None,
    ):
        pos = await self.get_position(position_id)
        if not pos:
            return

        close_qty = pos["qty"] if qty is None else min(qty, pos["qty"])
        if close_qty <= 0:
            return
        remaining_qty = max(pos["qty"] - close_qty, 0.0)
        fully_closed = remaining_qty <= 1e-12

        direction = 1.0 if pos["side"] == "LONG" else -1.0
        leverage = pos["leverage"] or 1
        fee_pct = pos.get("fee_pct") or 0.0
        exchange = pos.get("exchange") or "binance"
        gross_pnl = (exit_price - pos["entry_price"]) * close_qty * direction
        fee = (pos["entry_price"] + exit_price) * close_qty * fee_pct
        pnl = gross_pnl - fee
        capital = pos["entry_price"] * close_qty
        pnl_percent = pnl / capital * 100.0 if capital else 0.0

        opened = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
        closed = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        duration_hours = (closed - opened).total_seconds() / 3600.0

        trade_metadata = merge_trade_metadata(pos.get("metadata"), close_metadata)
        trade_id = pos["position_id"] if fully_closed else str(uuid.uuid4())

        await self._conn.execute(
            """INSERT INTO trades
               (trade_id, position_id, alpha_id, signal_id, symbol, side,
                entry_price, exit_price, qty, pnl, pnl_percent, leverage,
                tp, sl, reason, duration_hours, opened_at, closed_at, metadata, fee, exchange)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade_id,
                pos["position_id"],
                pos["alpha_id"],
                pos["signal_id"],
                pos["symbol"],
                pos["side"],
                pos["entry_price"],
                exit_price,
                close_qty,
                pnl,
                pnl_percent,
                pos["leverage"],
                pos["tp"],
                pos["sl"],
                reason,
                duration_hours,
                pos["opened_at"],
                closed_at,
                trade_metadata,
                fee,
                exchange,
            ),
        )
        if fully_closed:
            await self._conn.execute(
                "DELETE FROM positions WHERE position_id = ?", (position_id,)
            )
        else:
            await self._conn.execute(
                "UPDATE positions SET qty = ? WHERE position_id = ?",
                (remaining_qty, position_id),
            )
        await self._commit()

    async def get_trade(self, position_id: str):
        cursor = await self._conn.execute(
            "SELECT * FROM trades WHERE position_id = ?", (position_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_trades_by_alpha(self, alpha_id: str, limit: int = 100, offset: int = 0):
        cursor = await self._conn.execute(
            "SELECT * FROM trades WHERE alpha_id = ? ORDER BY closed_at DESC LIMIT ? OFFSET ?",
            (alpha_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def log_signal(self, signal_id: str, alpha_id: str, signal_type: str, payload: str):
        now = datetime.utcnow().isoformat()
        await self._conn.execute(
            """INSERT INTO signals (signal_id, alpha_id, type, payload, received_at, processed)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (signal_id, alpha_id, signal_type, payload, now),
        )
        await self._commit()

    async def mark_signal_processed(self, signal_id: str, error: str = None):
        await self._conn.execute(
            "UPDATE signals SET processed = 1, error = ? WHERE signal_id = ?",
            (error, signal_id),
        )
        await self._commit()

    async def get_signals(self, alpha_id: str = None, limit: int = 100):
        if alpha_id:
            cursor = await self._conn.execute(
                "SELECT * FROM signals WHERE alpha_id = ? ORDER BY received_at DESC LIMIT ?",
                (alpha_id, limit),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM signals ORDER BY received_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_all_alphas(self):
        cursor = await self._conn.execute("SELECT * FROM alphas ORDER BY created_at")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_all_open_positions(self, alpha_id: str = None):
        if alpha_id:
            cursor = await self._conn.execute(
                "SELECT * FROM positions WHERE alpha_id = ? ORDER BY opened_at DESC",
                (alpha_id,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM positions ORDER BY opened_at DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_symbols_with_open_positions(self) -> list[str]:
        cursor = await self._conn.execute("SELECT DISTINCT symbol FROM positions")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def get_symbol_exchange_map(self) -> dict[str, str]:
        cursor = await self._conn.execute(
            "SELECT DISTINCT symbol, exchange FROM positions"
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] or "binance" for row in rows}

    async def get_open_symbols_by_exchange(self) -> dict[str, set[str]]:
        cursor = await self._conn.execute(
            "SELECT DISTINCT exchange, symbol FROM positions"
        )
        rows = await cursor.fetchall()
        result: dict[str, set[str]] = {}
        for exchange, symbol in rows:
            result.setdefault((exchange or "binance").lower(), set()).add(symbol)
        return result

    async def get_positions_with_tpsl(self) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM positions WHERE tp IS NOT NULL OR sl IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

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
        await self._commit()

    async def prune_signals(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cursor = await self._conn.execute(
            "DELETE FROM signals WHERE received_at < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        await self._commit()
        return cursor.rowcount

    async def get_alpha_columns(self, alpha_id: str) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM alpha_columns WHERE alpha_id = ? ORDER BY sort_order",
            (alpha_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
