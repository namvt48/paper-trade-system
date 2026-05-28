import pytest
import json
from app.db import Database
from app.executor import Executor
from app.main import process_signal_message


@pytest.fixture
async def setup(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    executor = Executor(database, slippage_pct=0.0)
    yield database, executor
    await database.close()


@pytest.mark.asyncio
async def test_process_signal_message_open(setup):
    db, executor = setup
    data = {
        "type": "OPEN",
        "alpha_id": "test-alpha",
        "signal_id": "sig-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "95000.0",
        "qty": "0.01",
        "tp": "97000.0",
        "sl": "94000.0",
        "leverage": "10",
        "metadata": "{}",
        "timestamp": "2026-05-22T10:00:00Z",
    }
    result = await process_signal_message(data, db, executor)
    assert result["position_id"] is not None
    signals = await db.get_signals(alpha_id="test-alpha")
    assert len(signals) == 1
    assert signals[0]["processed"] == 1


@pytest.mark.asyncio
async def test_process_signal_message_modify(setup):
    db, executor = setup
    open_data = {
        "type": "OPEN",
        "alpha_id": "test-alpha",
        "signal_id": "sig-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "95000.0",
        "qty": "0.01",
        "sl": "94000.0",
        "timestamp": "2026-05-22T10:00:00Z",
    }
    result = await process_signal_message(open_data, db, executor)

    modify_data = {
        "type": "MODIFY",
        "alpha_id": "test-alpha",
        "signal_id": "sig-002",
        "position_id": result["position_id"],
        "sl": "94500.0",
        "timestamp": "2026-05-22T10:30:00Z",
    }
    await process_signal_message(modify_data, db, executor)
    pos = await db.get_position(result["position_id"])
    assert pos["sl"] == 94500.0


@pytest.mark.asyncio
async def test_process_signal_message_close(setup):
    db, executor = setup
    open_data = {
        "type": "OPEN",
        "alpha_id": "test-alpha",
        "signal_id": "sig-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "95000.0",
        "qty": "0.01",
        "leverage": "10",
        "timestamp": "2026-05-22T10:00:00Z",
    }
    result = await process_signal_message(open_data, db, executor)

    close_data = {
        "type": "CLOSE",
        "alpha_id": "test-alpha",
        "signal_id": "sig-002",
        "position_id": result["position_id"],
        "reason": "SIGNAL",
        "exit_price": "96000.0",
        "timestamp": "2026-05-22T11:00:00Z",
    }
    await process_signal_message(close_data, db, executor)
    trade = await db.get_trade(result["position_id"])
    assert trade is not None


@pytest.mark.asyncio
async def test_process_signal_message_logs_error(setup):
    db, executor = setup
    data = {
        "type": "CLOSE",
        "alpha_id": "test-alpha",
        "signal_id": "sig-999",
        "position_id": "nonexistent",
        "reason": "SIGNAL",
        "exit_price": "96000.0",
        "timestamp": "2026-05-22T11:00:00Z",
    }
    result = await process_signal_message(data, db, executor)
    assert result is None
    signals = await db.get_signals(alpha_id="test-alpha")
    assert len(signals) == 1
    assert signals[0]["processed"] == 1
    assert signals[0]["error"] is not None
