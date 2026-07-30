"""Regression: shadow (book_only sleeve) rows must surface realized PnL.

2026-07-27: the dashboard's "realized" panel showed nothing for every
`*-sleeve` alpha. `EquitySnapshotCollector.snapshot_once()` hardcoded
`realized_pnl=0.0` for shadow rows and dumped the entire balance move into
`unrealized_pnl`, even though closed virtual trades (with a real `pnl`
column, written on every VIRTUAL_CLOSE) already existed in
`virtual_trades` -- the same data source `trades.pnl` provides for real
alphas via `_get_realized_pnl_by_alpha`.
"""

from __future__ import annotations

import json

import pytest

from app.db import Database
from app.equity_snapshots import EquitySnapshotCollector


class _FakeTickerCache:
    def get_price(self, symbol):
        return None


class _FakeAsyncRedis:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    async def keys(self, pattern: str):
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]

    async def get(self, key: str):
        return self.store.get(key)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "paper-trade.db"))
    await database.init()
    yield database
    await database.close()


async def _close_virtual_trade(
    db, alpha_id, entry, exit_price, qty, candle_a=1_000, candle_b=2_000
):
    opened = {
        "ledger_mode": "virtual",
        "type": "VIRTUAL_OPEN",
        "event_id": f"open-{alpha_id}-{candle_a}",
        "position_id": f"pos-{alpha_id}-{candle_a}",
        "alpha_id": alpha_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "price": entry,
        "qty": qty,
        "weight": 0.5,
        "exchange": "binance",
        "timeframe": "1h",
        "timestamp": "2026-07-27T00:00:00+00:00",
        "candle_open_ms": candle_a,
        "metadata": {"virtual": True},
    }
    closed = {
        **opened,
        "type": "VIRTUAL_CLOSE",
        "event_id": f"close-{alpha_id}-{candle_b}",
        "price": exit_price,
        "timestamp": "2026-07-27T01:00:00+00:00",
        "candle_open_ms": candle_b,
        "reason": "VIRTUAL_REBALANCE",
    }
    from app.virtual_trade_ledger import process_virtual_trade_message

    await process_virtual_trade_message({"payload": json.dumps(opened)}, db)
    await process_virtual_trade_message({"payload": json.dumps(closed)}, db)


@pytest.mark.asyncio
async def test_shadow_snapshot_surfaces_realized_pnl_from_virtual_trades(db, tmp_path):
    # entry=100, exit=110, qty=50 -> realized pnl = 500.0 (matches
    # test_virtual_trade_ledger.py's own math for the same inputs).
    await _close_virtual_trade(
        db, "test-sleeve", entry=100.0, exit_price=110.0, qty=50.0
    )

    redis_client = _FakeAsyncRedis(
        {
            "shadow:pnl:test-sleeve": json.dumps(
                {"alpha_id": "test-sleeve", "equity": 1.05, "capital": 10000.0}
            )
        }
    )
    collector = EquitySnapshotCollector(
        db=db,
        ticker_cache=_FakeTickerCache(),
        snapshot_db_path=str(tmp_path / "equity-snapshots.db"),
        alphas_dir=str(tmp_path / "no-alphas-here"),
        redis_client=redis_client,
    )
    await collector.init()
    await collector.snapshot_once()

    cursor = await collector._snap_conn.execute(
        "SELECT balance, unrealized_pnl, realized_pnl FROM equity_snapshots WHERE alpha_id=?",
        ("test-sleeve",),
    )
    row = await cursor.fetchone()
    await collector.close()

    balance, unrealized_pnl, realized_pnl = row
    assert balance == pytest.approx(10500.0)
    assert realized_pnl == pytest.approx(500.0)
    assert unrealized_pnl == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_shadow_snapshot_splits_realized_and_still_open_unrealized(db, tmp_path):
    # Same closed trade (realized=500) but current equity implies the sleeve
    # also holds an *unrealized* position on top of that -- the split must
    # not just dump everything into one bucket.
    await _close_virtual_trade(
        db, "test-sleeve", entry=100.0, exit_price=110.0, qty=50.0
    )

    redis_client = _FakeAsyncRedis(
        {
            "shadow:pnl:test-sleeve": json.dumps(
                {"alpha_id": "test-sleeve", "equity": 1.08, "capital": 10000.0}
            )
        }
    )
    collector = EquitySnapshotCollector(
        db=db,
        ticker_cache=_FakeTickerCache(),
        snapshot_db_path=str(tmp_path / "equity-snapshots.db"),
        alphas_dir=str(tmp_path / "no-alphas-here"),
        redis_client=redis_client,
    )
    await collector.init()
    await collector.snapshot_once()

    cursor = await collector._snap_conn.execute(
        "SELECT balance, unrealized_pnl, realized_pnl FROM equity_snapshots WHERE alpha_id=?",
        ("test-sleeve",),
    )
    row = await cursor.fetchone()
    await collector.close()

    balance, unrealized_pnl, realized_pnl = row
    assert balance == pytest.approx(10800.0)
    assert realized_pnl == pytest.approx(500.0)
    assert unrealized_pnl == pytest.approx(300.0)
