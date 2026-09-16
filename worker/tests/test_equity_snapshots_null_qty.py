"""Regression: NULL/non-finite qty or entry_price must not kill the collector.

SQLite coerces NaN to NULL. A NaN-qty OPEN (cross_alpha ZORAUSDT incident)
made `_compute_position_pnl` raise ``TypeError: unsupported operand type(s)
for *: 'float' and 'NoneType'`` every ~60s, so the equity collector produced
no snapshots for 57 hours. Corrupt rows must contribute ZERO PnL with one
aggregated warning; `balance = capital + realized + unrealized` for valid
rows must be unchanged.
"""

from __future__ import annotations

import logging

import pytest

from app.db import Database
from app.equity_snapshots import EquitySnapshotCollector


class _FakeTickerCache:
    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self.prices = dict(prices or {})

    def get_price(self, symbol):
        return self.prices.get(symbol)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "paper-trade.db"))
    await database.init()
    yield database
    await database.close()


async def _insert_position(db, alpha_id, symbol, entry_price, qty):
    await db._conn.execute(
        "INSERT OR IGNORE INTO alphas (alpha_id, display_name, created_at, status) "
        "VALUES (?, ?, ?, 'active')",
        (alpha_id, alpha_id, "2026-09-10T00:00:00+00:00"),
    )
    await db._conn.execute(
        "INSERT INTO positions (position_id, alpha_id, signal_id, symbol, side, "
        "entry_price, qty, tp, sl, leverage, opened_at, metadata, exchange, fee_pct) "
        "VALUES (?, ?, ?, ?, 'LONG', ?, ?, NULL, NULL, 1, ?, '{}', 'binance', 0.0)",
        (
            f"pos-{symbol}",
            alpha_id,
            f"sig-{symbol}",
            symbol,
            entry_price,
            qty,
            "2026-09-10T00:00:00+00:00",
        ),
    )
    await db._conn.commit()


@pytest.mark.asyncio
async def test_snapshot_survives_null_qty_and_keeps_valid_math(db, tmp_path, caplog):
    await _insert_position(db, "test-alpha", "ZORAUSDT", 0.05, None)
    await _insert_position(db, "test-alpha", "BTCUSDT", 100.0, 1.0)

    collector = EquitySnapshotCollector(
        db=db,
        ticker_cache=_FakeTickerCache({"ZORAUSDT": 0.06, "BTCUSDT": 110.0}),
        snapshot_db_path=str(tmp_path / "equity-snapshots.db"),
        alphas_dir=str(tmp_path / "no-alphas-here"),
    )
    await collector.init()
    with caplog.at_level(logging.WARNING, logger="app.equity_snapshots"):
        await collector.snapshot_once()

    cursor = await collector._snap_conn.execute(
        "SELECT balance, unrealized_pnl, realized_pnl FROM equity_snapshots WHERE alpha_id=?",
        ("test-alpha",),
    )
    row = await cursor.fetchone()
    await collector.close()

    balance, unrealized_pnl, realized_pnl = row
    assert realized_pnl == pytest.approx(0.0)
    assert unrealized_pnl == pytest.approx(10.0)
    assert balance == pytest.approx(10000.0 + 0.0 + 10.0)

    warnings = [r for r in caplog.records if "NULL/non-finite" in r.getMessage()]
    assert len(warnings) == 1
    assert "ZORAUSDT" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_compute_position_pnl_zero_for_corrupt_rows():
    for corrupt in (
        {"side": "LONG", "entry_price": None, "qty": 1.0},
        {"side": "LONG", "entry_price": 100.0, "qty": None},
        {"side": "LONG", "entry_price": float("nan"), "qty": 1.0},
        {"side": "LONG", "entry_price": 100.0, "qty": float("inf")},
    ):
        assert EquitySnapshotCollector._compute_position_pnl(corrupt, 110.0) == 0.0

    valid = {"side": "LONG", "entry_price": 100.0, "qty": 1.0, "fee_pct": 0.0}
    assert EquitySnapshotCollector._compute_position_pnl(valid, 110.0) == pytest.approx(
        10.0
    )


@pytest.mark.asyncio
async def test_shadow_mark_survives_null_qty(db, tmp_path):
    import json

    corrupt_virtual = [
        {
            "alpha_id": "test-sleeve",
            "symbol": "ZORAUSDT",
            "side": "LONG",
            "entry_price": 0.05,
            "qty": None,
            "fee_pct": 0.0,
        }
    ]

    async def _fake_get_virtual_positions():
        return corrupt_virtual

    db.get_virtual_positions = _fake_get_virtual_positions

    class _FakeAsyncRedis:
        def __init__(self, store):
            self.store = store

        async def keys(self, pattern):
            prefix = pattern.rstrip("*")
            return [k for k in self.store if k.startswith(prefix)]

        async def get(self, key):
            return self.store.get(key)

    redis_client = _FakeAsyncRedis(
        {
            "shadow:pnl:test-sleeve": json.dumps(
                {
                    "alpha_id": "test-sleeve",
                    "equity": 1.05,
                    "capital": 10000.0,
                    "prices": {"ZORAUSDT": 0.05},
                }
            )
        }
    )
    collector = EquitySnapshotCollector(
        db=db,
        ticker_cache=_FakeTickerCache({"ZORAUSDT": 0.06}),
        snapshot_db_path=str(tmp_path / "equity-snapshots.db"),
        alphas_dir=str(tmp_path / "no-alphas-here"),
        redis_client=redis_client,
    )
    await collector.init()
    await collector.snapshot_once()

    cursor = await collector._snap_conn.execute(
        "SELECT balance FROM equity_snapshots WHERE alpha_id=?",
        ("test-sleeve",),
    )
    row = await cursor.fetchone()
    await collector.close()

    assert row is not None
    assert row[0] == pytest.approx(10500.0)
