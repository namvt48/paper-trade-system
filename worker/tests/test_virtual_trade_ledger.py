from __future__ import annotations

import pytest

from app.db import Database
from app.virtual_trade_ledger import _contains_rows, process_virtual_trade_message


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "virtual-ledger.db"))
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_virtual_open_close_creates_trade_without_real_position(db):
    opened = {
        "ledger_mode": "virtual",
        "type": "VIRTUAL_OPEN",
        "event_id": "open-1",
        "position_id": "virtual-position-1",
        "alpha_id": "sleeve-a",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "price": 100.0,
        "qty": 50.0,
        "weight": 0.5,
        "exchange": "binance",
        "timeframe": "1h",
        "timestamp": "2026-07-24T00:00:00+00:00",
        "candle_open_ms": 1_000,
        "metadata": {"virtual": True},
    }
    closed = {
        **opened,
        "type": "VIRTUAL_CLOSE",
        "event_id": "close-1",
        "price": 110.0,
        "timestamp": "2026-07-25T00:00:00+00:00",
        "candle_open_ms": 2_000,
        "reason": "VIRTUAL_REBALANCE",
    }

    await process_virtual_trade_message({"payload": __import__("json").dumps(opened)}, db)
    await process_virtual_trade_message({"payload": __import__("json").dumps(closed)}, db)
    await process_virtual_trade_message({"payload": __import__("json").dumps(closed)}, db)

    real_positions = await db.get_all_open_positions("sleeve-a")
    virtual_positions = await db.get_virtual_positions("sleeve-a")
    virtual_trades = await db.get_virtual_trades_by_alpha("sleeve-a")
    assert real_positions == []
    assert virtual_positions == []
    assert len(virtual_trades) == 1
    assert virtual_trades[0]["pnl"] == pytest.approx(500.0)
    assert virtual_trades[0]["pnl_percent"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_non_virtual_message_is_rejected(db):
    with pytest.raises(ValueError, match="ledger_mode"):
        await process_virtual_trade_message(
            {"payload": '{"ledger_mode":"real","type":"OPEN"}'},
            db,
        )


def test_empty_pending_stream_response_switches_to_new_messages():
    assert not _contains_rows([("paper-shadow-trades", [])])
    assert _contains_rows(
        [("paper-shadow-trades", [("1-0", {"payload": "{}"})])]
    )
