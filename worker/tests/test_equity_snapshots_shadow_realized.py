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
    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self.prices = dict(prices or {})

    def get_price(self, symbol):
        return self.prices.get(symbol)

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price


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


@pytest.mark.asyncio
async def test_shadow_snapshot_marks_open_virtual_positions_to_live_price(db, tmp_path):
    opened = {
        "ledger_mode": "virtual",
        "type": "VIRTUAL_OPEN",
        "event_id": "open-live-mark",
        "position_id": "pos-live-mark",
        "alpha_id": "test-sleeve",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "price": 90.0,
        "qty": 10.0,
        "weight": 0.09,
        "exchange": "binance",
        "timeframe": "1h",
        "timestamp": "2026-07-27T00:00:00+00:00",
        "candle_open_ms": 1_000,
        "metadata": {"virtual": True},
    }
    from app.virtual_trade_ledger import process_virtual_trade_message

    await process_virtual_trade_message({"payload": json.dumps(opened)}, db)

    # The runner's persisted shadow NAV was marked at BTC=100 and already
    # contains $200 of total unrealized/cost carry. The collector must preserve
    # that anchor, then add only the open position's move after the anchor.
    redis_client = _FakeAsyncRedis(
        {
            "shadow:pnl:test-sleeve": json.dumps(
                {
                    "alpha_id": "test-sleeve",
                    "equity": 1.02,
                    "capital": 10000.0,
                    "prices": {"BTCUSDT": 100.0},
                }
            )
        }
    )
    ticker = _FakeTickerCache({"BTCUSDT": 110.0})
    collector = EquitySnapshotCollector(
        db=db,
        ticker_cache=ticker,
        snapshot_db_path=str(tmp_path / "equity-snapshots.db"),
        alphas_dir=str(tmp_path / "no-alphas-here"),
        redis_client=redis_client,
    )
    await collector.init()

    await collector.snapshot_once()
    ticker.set_price("BTCUSDT", 120.0)
    await collector.snapshot_once()

    cursor = await collector._snap_conn.execute(
        """
        SELECT balance, unrealized_pnl, realized_pnl
        FROM equity_snapshots
        WHERE alpha_id=?
        ORDER BY id
        """,
        ("test-sleeve",),
    )
    rows = await cursor.fetchall()
    await collector.close()

    assert [row["balance"] for row in rows] == pytest.approx([10300.0, 10400.0])
    assert [row["unrealized_pnl"] for row in rows] == pytest.approx([300.0, 400.0])
    assert [row["realized_pnl"] for row in rows] == pytest.approx([0.0, 0.0])
