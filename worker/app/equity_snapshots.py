from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

TOTAL_KEY = "__TOTAL__"


def _parse_env_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                trimmed = line.strip()
                if not trimmed or trimmed.startswith("#"):
                    continue
                eq = trimmed.find("=")
                if eq == -1:
                    continue
                key = trimmed[:eq].strip()
                val = trimmed[eq + 1 :].split("#")[0].strip()
                result[key] = val
    except OSError:
        pass
    return result


def load_alpha_capitals(alphas_dir: str) -> dict[str, float]:
    capitals: dict[str, float] = {}
    try:
        entries = os.listdir(alphas_dir)
    except OSError:
        return capitals
    for entry in entries:
        env_path = os.path.join(alphas_dir, entry, ".env")
        if not os.path.isfile(env_path):
            continue
        parsed = _parse_env_file(env_path)
        alpha_id = parsed.get("ALPHA_ID", entry)
        capital_str = parsed.get("CAPITAL", "10000.0")
        try:
            capitals[alpha_id] = float(capital_str)
        except ValueError:
            capitals[alpha_id] = 10000.0
    return capitals


class LastPriceCache:
    """Holds the most recent price per symbol. Never returns None after first sighting."""

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}

    def update(self, symbol: str, price: float) -> None:
        if price > 0:
            self._prices[symbol] = price

    def get(self, symbol: str) -> Optional[float]:
        return self._prices.get(symbol)


class EquitySnapshotCollector:
    def __init__(
        self,
        db,
        ticker_cache,
        snapshot_db_path: str,
        interval_sec: float = 300.0,
        alphas_dir: str = "alphas",
        redis_client=None,
    ) -> None:
        self._db = db
        self._ticker_cache = ticker_cache
        self._snapshot_db_path = snapshot_db_path
        self._interval_sec = interval_sec
        self._alphas_dir = alphas_dir
        self._redis_client = redis_client
        self._last_prices = LastPriceCache()
        self._snap_conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self._snapshot_db_path) or ".", exist_ok=True)
        self._snap_conn = await aiosqlite.connect(self._snapshot_db_path)
        self._snap_conn.row_factory = aiosqlite.Row
        await self._snap_conn.execute("PRAGMA journal_mode=WAL")
        await self._snap_conn.execute("PRAGMA synchronous=NORMAL")
        await self._snap_conn.execute("PRAGMA busy_timeout=5000")
        await self._snap_conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                alpha_id TEXT NOT NULL,
                balance REAL NOT NULL,
                unrealized_pnl REAL DEFAULT 0,
                realized_pnl REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_equity_snapshots_alpha_time
                ON equity_snapshots(alpha_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_equity_snapshots_time
                ON equity_snapshots(timestamp);
            """
        )
        for col in ("unrealized_pnl", "realized_pnl"):
            try:
                await self._snap_conn.execute(
                    f"ALTER TABLE equity_snapshots ADD COLUMN {col} REAL DEFAULT 0"
                )
            except aiosqlite.OperationalError:
                pass
        await self._snap_conn.commit()
        logger.info(
            "[EQUITY-SNAPSHOT] initialised db=%s interval=%.0fs",
            self._snapshot_db_path,
            self._interval_sec,
        )

    async def close(self) -> None:
        if self._snap_conn:
            await self._snap_conn.close()

    def _get_price(self, symbol: str) -> Optional[float]:
        live = self._ticker_cache.get_price(symbol)
        if live is not None:
            self._last_prices.update(symbol, live)
            return live
        return self._last_prices.get(symbol)

    @staticmethod
    def _compute_position_pnl(position: dict[str, Any], current_price: float) -> float:
        direction = 1.0 if position["side"] == "LONG" else -1.0
        gross = (current_price - position["entry_price"]) * position["qty"] * direction
        fee = (
            (position["entry_price"] + current_price)
            * position["qty"]
            * (position.get("fee_pct") or 0.0)
        )
        return gross - fee

    async def _get_realized_pnl_by_alpha(self) -> dict[str, float]:
        cursor = await self._db._conn.execute(
            "SELECT alpha_id, COALESCE(SUM(pnl), 0) as total FROM trades GROUP BY alpha_id"
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def _get_virtual_realized_pnl_by_alpha(self) -> dict[str, float]:
        cursor = await self._db._conn.execute(
            "SELECT alpha_id, COALESCE(SUM(pnl), 0) as total FROM virtual_trades GROUP BY alpha_id"
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def _get_shadow_pnl_by_alpha(self) -> dict[str, dict[str, float]]:
        if self._redis_client is None:
            return {}
        result: dict[str, dict[str, float]] = {}
        try:
            for key in await self._redis_client.keys("shadow:pnl:*"):
                raw = await self._redis_client.get(key)
                if not raw:
                    continue
                payload = json.loads(raw)
                alpha_id = str(payload.get("alpha_id") or str(key).rsplit(":", 1)[-1])
                equity = float(payload.get("equity", 1.0))
                capital = float(payload.get("capital", 10000.0))
                if equity > 0 and capital > 0:
                    result[alpha_id] = {"balance": capital * equity, "capital": capital}
        except Exception:
            logger.exception("[EQUITY-SNAPSHOT] shadow-PnL read failed")
        return result

    async def snapshot_once(self) -> None:
        if self._snap_conn is None:
            return

        positions = await self._db.get_all_open_positions()
        realized = await self._get_realized_pnl_by_alpha()
        virtual_realized = await self._get_virtual_realized_pnl_by_alpha()
        shadow = await self._get_shadow_pnl_by_alpha()
        capitals = load_alpha_capitals(self._alphas_dir)

        unrealized_by_alpha: dict[str, float] = {}
        missing_prices: list[str] = []

        for pos in positions:
            symbol = pos["symbol"]
            price = self._get_price(symbol)
            if price is None:
                missing_prices.append(symbol)
                continue
            pnl = self._compute_position_pnl(pos, price)
            alpha_id = pos["alpha_id"]
            unrealized_by_alpha[alpha_id] = unrealized_by_alpha.get(alpha_id, 0.0) + pnl

        if missing_prices:
            logger.warning(
                "[EQUITY-SNAPSHOT] no price for %d symbols: %s",
                len(missing_prices),
                ", ".join(sorted(set(missing_prices))[:10]),
            )

        all_alphas = await self._db.get_all_alphas()
        alpha_ids = {row["alpha_id"] for row in all_alphas}
        alpha_ids.update(realized.keys())
        alpha_ids.update(unrealized_by_alpha.keys())
        shadow_ids = set(shadow)
        alpha_ids.difference_update(shadow_ids)

        ts = datetime.now(timezone.utc).isoformat()
        total_balance = 0.0
        total_unrealized = 0.0
        total_realized = 0.0

        for alpha_id in sorted(alpha_ids):
            cap = capitals.get(alpha_id, 10000.0)
            real = realized.get(alpha_id, 0.0)
            unreal = unrealized_by_alpha.get(alpha_id, 0.0)
            balance = cap + real + unreal
            total_balance += balance
            total_unrealized += unreal
            total_realized += real

            await self._snap_conn.execute(
                "INSERT INTO equity_snapshots (timestamp, alpha_id, balance, unrealized_pnl, realized_pnl) VALUES (?, ?, ?, ?, ?)",
                (ts, alpha_id, balance, unreal, real),
            )

        # Shadow sleeves have no worker positions/trades. Their rows are
        # standalone 100%-basis NAV, intentionally excluded from TOTAL_KEY.
        # realized_pnl comes from closed virtual trades (virtual_trades.pnl,
        # written on each VIRTUAL_CLOSE) -- the rest of the balance move
        # since capital is the still-open sleeve's unrealized_pnl. Previously
        # this hardcoded realized_pnl=0.0 and dumped everything into
        # unrealized_pnl, so the dashboard's realized column stayed empty
        # for every sleeve despite virtual_trades already having the data.
        for alpha_id, values in sorted(shadow.items()):
            balance = values["balance"]
            capital = values["capital"]
            real = virtual_realized.get(alpha_id, 0.0)
            unreal = balance - capital - real
            await self._snap_conn.execute(
                "INSERT INTO equity_snapshots (timestamp, alpha_id, balance, unrealized_pnl, realized_pnl) VALUES (?, ?, ?, ?, ?)",
                (ts, alpha_id, balance, unreal, real),
            )

        await self._snap_conn.execute(
            "INSERT INTO equity_snapshots (timestamp, alpha_id, balance, unrealized_pnl, realized_pnl) VALUES (?, ?, ?, ?, ?)",
            (ts, TOTAL_KEY, total_balance, total_unrealized, total_realized),
        )
        await self._snap_conn.commit()

    async def run(self) -> None:
        while True:
            try:
                await self.snapshot_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[EQUITY-SNAPSHOT] snapshot failed")
            await asyncio.sleep(self._interval_sec)
