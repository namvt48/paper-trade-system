import pytest
from app.db import Database
from app.executor import Executor
from app.main import process_signal_message


@pytest.fixture
async def integration_setup(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    executor = Executor(database, slippage_pct=0.0)
    yield database, executor
    await database.close()


@pytest.mark.asyncio
async def test_full_signal_lifecycle(integration_setup):
    db, executor = integration_setup

    open_msg = {
        "type": "OPEN",
        "alpha_id": "the-leader-1",
        "signal_id": "sig-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "95000.0",
        "qty": "0.01",
        "tp": "97000.0",
        "sl": "94000.0",
        "leverage": "10",
        "metadata": '{"mode": "NORMAL"}',
        "timestamp": "2026-05-22T10:00:00Z",
    }
    result = await process_signal_message(open_msg, db, executor)
    assert result["position_id"] is not None
    pos_id = result["position_id"]

    alpha = await db.get_alpha("the-leader-1")
    assert alpha is not None
    assert alpha["status"] == "active"

    pos = await db.get_position(pos_id)
    assert pos["symbol"] == "BTCUSDT"
    assert pos["tp"] == 97000.0

    modify_msg = {
        "type": "MODIFY",
        "alpha_id": "the-leader-1",
        "signal_id": "sig-002",
        "position_id": pos_id,
        "sl": "94500.0",
        "timestamp": "2026-05-22T10:30:00Z",
    }
    result = await process_signal_message(modify_msg, db, executor)
    assert result["modified"] is True

    pos = await db.get_position(pos_id)
    assert pos["sl"] == 94500.0

    close_msg = {
        "type": "CLOSE",
        "alpha_id": "the-leader-1",
        "signal_id": "sig-003",
        "position_id": pos_id,
        "reason": "TP_HIT",
        "exit_price": "97000.0",
        "timestamp": "2026-05-22T11:00:00Z",
    }
    result = await process_signal_message(close_msg, db, executor)
    assert result["closed"] is True

    trade = await db.get_trade(pos_id)
    assert trade["pnl"] == pytest.approx(2.0)
    assert trade["reason"] == "TP_HIT"
    assert trade["sl"] == 94500.0

    signals = await db.get_signals(alpha_id="the-leader-1")
    assert len(signals) == 3
    assert all(s["processed"] == 1 for s in signals)


@pytest.mark.asyncio
async def test_multiple_alphas(integration_setup):
    db, executor = integration_setup

    for alpha_name in ["alpha-a", "alpha-b"]:
        msg = {
            "type": "OPEN",
            "alpha_id": alpha_name,
            "signal_id": f"sig-{alpha_name}",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry": "95000.0",
            "qty": "0.01",
            "timestamp": "2026-05-22T10:00:00Z",
        }
        await process_signal_message(msg, db, executor)

    positions = await db.get_all_open_positions()
    assert len(positions) == 2

    alphas = await db.get_all_alphas()
    assert len(alphas) == 2


@pytest.mark.asyncio
async def test_signal_error_does_not_crash(integration_setup):
    db, executor = integration_setup

    bad_msg = {
        "type": "CLOSE",
        "alpha_id": "nonexistent",
        "signal_id": "sig-bad",
        "position_id": "nonexistent",
        "reason": "SIGNAL",
        "timestamp": "2026-05-22T11:00:00Z",
    }
    result = await process_signal_message(bad_msg, db, executor)
    assert result is None

    good_msg = {
        "type": "OPEN",
        "alpha_id": "test-alpha",
        "signal_id": "sig-good",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "95000.0",
        "qty": "0.01",
        "timestamp": "2026-05-22T10:00:00Z",
    }
    result = await process_signal_message(good_msg, db, executor)
    assert result["position_id"] is not None


@pytest.mark.asyncio
async def test_active_tpsl_close_on_tp_hit(integration_setup):
    db, executor = integration_setup

    open_data = {
        "type": "OPEN", "alpha_id": "test-alpha", "signal_id": "sig-001",
        "symbol": "BTCUSDT", "side": "LONG", "entry": "95000.0", "qty": "0.01",
        "tp": "97000.0", "sl": "94000.0", "leverage": "10", "metadata": "{}",
        "timestamp": "2026-05-22T10:00:00Z",
    }
    await process_signal_message(open_data, db, executor)

    hits = await executor.check_tpsl_hits({"BTCUSDT": 97500.0})
    assert len(hits) == 1
    assert hits[0]["reason"] == "TP_HIT"

    trade = await db.get_trade(hits[0]["position_id"])
    assert trade is not None
    assert trade["reason"] == "TP_HIT"


@pytest.mark.asyncio
async def test_active_tpsl_close_on_sl_hit(integration_setup):
    db, executor = integration_setup

    open_data = {
        "type": "OPEN", "alpha_id": "test-alpha", "signal_id": "sig-001",
        "symbol": "BTCUSDT", "side": "LONG", "entry": "95000.0", "qty": "0.01",
        "tp": "97000.0", "sl": "94000.0", "leverage": "10", "metadata": "{}",
        "timestamp": "2026-05-22T10:00:00Z",
    }
    await process_signal_message(open_data, db, executor)

    hits = await executor.check_tpsl_hits({"BTCUSDT": 93500.0})
    assert len(hits) == 1
    assert hits[0]["reason"] == "SL_HIT"


@pytest.mark.asyncio
async def test_alpha_never_pushes_close_active_tpsl(integration_setup):
    db, executor = integration_setup

    open_data = {
        "type": "OPEN", "alpha_id": "test-alpha", "signal_id": "sig-001",
        "symbol": "ETHUSDT", "side": "SHORT", "entry": "3000.0", "qty": "1.0",
        "tp": "2900.0", "sl": "3100.0", "leverage": "10", "metadata": "{}",
        "timestamp": "2026-05-22T10:00:00Z",
    }
    await process_signal_message(open_data, db, executor)

    pos = await db.get_open_position_by_alpha_symbol("test-alpha", "ETHUSDT")
    modify_data = {
        "type": "MODIFY", "alpha_id": "test-alpha", "signal_id": "sig-002",
        "position_id": pos["position_id"],
        "sl": "3050.0", "timestamp": "2026-05-22T10:30:00Z",
    }
    await process_signal_message(modify_data, db, executor)

    hits = await executor.check_tpsl_hits({"ETHUSDT": 2890.0})
    assert len(hits) == 1
    assert hits[0]["reason"] == "TP_HIT"

    trade = await db.get_trade(hits[0]["position_id"])
    assert trade["sl"] == 3050.0
