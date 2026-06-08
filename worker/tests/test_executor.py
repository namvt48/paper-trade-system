import json
import pytest
from app.db import Database
from app.executor import Executor
from app.models import OpenSignal, ModifySignal, CloseSignal, RegisterColumnsSignal, SignalType


@pytest.fixture
async def executor(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    exec = Executor(database, slippage_pct=0.0)
    yield exec
    await database.close()


@pytest.mark.asyncio
async def test_process_open_signal(executor):
    signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        timestamp="2026-05-22T10:00:00Z",
        tp=97000.0,
        sl=94000.0,
        leverage=10,
    )
    result = await executor.process_open(signal)
    assert result["position_id"] is not None
    assert result["fill_price"] == 95000.0
    pos = await executor.db.get_position(result["position_id"])
    assert pos is not None
    assert pos["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_process_open_auto_registers_alpha(executor):
    signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="new-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        timestamp="2026-05-22T10:00:00Z",
    )
    await executor.process_open(signal)
    alpha = await executor.db.get_alpha("new-alpha")
    assert alpha is not None
    assert alpha["status"] == "active"


@pytest.mark.asyncio
async def test_process_open_duplicate_reject(executor):
    signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        timestamp="2026-05-22T10:00:00Z",
    )
    await executor.process_open(signal)
    with pytest.raises(ValueError, match="already has an open position"):
        await executor.process_open(signal)


@pytest.mark.asyncio
async def test_process_open_with_slippage(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    exec = Executor(database, slippage_pct=0.1)
    signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await exec.process_open(signal)
    assert result["fill_price"] == pytest.approx(95009.5)
    await database.close()


@pytest.mark.asyncio
async def test_process_modify_signal(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        sl=94000.0,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    modify_signal = ModifySignal(
        type=SignalType.MODIFY,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id=result["position_id"],
        tp=97000.0,
        sl=94500.0,
        timestamp="2026-05-22T10:30:00Z",
    )
    await executor.process_modify(modify_signal)
    pos = await executor.db.get_position(result["position_id"])
    assert pos["tp"] == 97000.0
    assert pos["sl"] == 94500.0


@pytest.mark.asyncio
async def test_process_modify_trailing_sl_long(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        sl=94000.0,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)

    modify_signal = ModifySignal(
        type=SignalType.MODIFY,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id=result["position_id"],
        sl=94500.0,
        timestamp="2026-05-22T10:30:00Z",
    )
    await executor.process_modify(modify_signal)

    modify_signal2 = ModifySignal(
        type=SignalType.MODIFY,
        alpha_id="test-alpha",
        signal_id="sig-003",
        position_id=result["position_id"],
        sl=94200.0,
        timestamp="2026-05-22T10:45:00Z",
    )
    with pytest.raises(ValueError, match="Trailing SL cannot move"):
        await executor.process_modify(modify_signal2)


@pytest.mark.asyncio
async def test_process_modify_trailing_sl_short(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="SHORT",
        entry=95000.0,
        qty=0.01,
        sl=96000.0,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)

    modify_signal = ModifySignal(
        type=SignalType.MODIFY,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id=result["position_id"],
        sl=95500.0,
        timestamp="2026-05-22T10:30:00Z",
    )
    await executor.process_modify(modify_signal)

    modify_signal2 = ModifySignal(
        type=SignalType.MODIFY,
        alpha_id="test-alpha",
        signal_id="sig-003",
        position_id=result["position_id"],
        sl=95800.0,
        timestamp="2026-05-22T10:45:00Z",
    )
    with pytest.raises(ValueError, match="Trailing SL cannot move"):
        await executor.process_modify(modify_signal2)


@pytest.mark.asyncio
async def test_process_close_signal(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        leverage=10,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    close_signal = CloseSignal(
        type=SignalType.CLOSE,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id=result["position_id"],
        reason="SIGNAL",
        exit_price=96000.0,
        timestamp="2026-05-22T11:00:00Z",
    )
    await executor.process_close(close_signal)
    pos = await executor.db.get_position(result["position_id"])
    assert pos is None
    trade = await executor.db.get_trade(result["position_id"])
    assert trade["pnl"] == pytest.approx(10.0)
    assert trade["reason"] == "SIGNAL"


@pytest.mark.asyncio
async def test_process_partial_close_signal(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        leverage=10,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    close_signal = CloseSignal(
        type=SignalType.CLOSE,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id=result["position_id"],
        reason="TP1",
        exit_price=96000.0,
        qty=0.0075,
        timestamp="2026-05-22T11:00:00Z",
    )

    close_result = await executor.process_close(close_signal)

    pos = await executor.db.get_position(result["position_id"])
    assert pos["qty"] == pytest.approx(0.0025)
    assert close_result["closed"] is False
    assert close_result["remaining_qty"] == pytest.approx(0.0025)
    trades = await executor.db.get_trades_by_alpha("test-alpha")
    assert len(trades) == 1
    assert trades[0]["position_id"] == result["position_id"]
    assert trades[0]["qty"] == pytest.approx(0.0075)
    assert trades[0]["reason"] == "TP1"


@pytest.mark.asyncio
async def test_process_partial_close_rejects_qty_above_position(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    close_signal = CloseSignal(
        type=SignalType.CLOSE,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id=result["position_id"],
        reason="TP1",
        exit_price=96000.0,
        qty=0.02,
        timestamp="2026-05-22T11:00:00Z",
    )

    with pytest.raises(ValueError, match="exceeds open qty"):
        await executor.process_close(close_signal)


@pytest.mark.asyncio
async def test_partial_close_legs_then_full_close(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="hyper-turbo",
        signal_id="sig-open",
        symbol="BTCUSDT",
        side="LONG",
        entry=100.0,
        qty=30.0,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)

    for signal_id, reason, qty, price in (
        ("sig-tp1", "TP1", 22.5, 110.0),
        ("sig-tp2", "TP2", 3.75, 112.0),
    ):
        await executor.process_close(CloseSignal(
            type=SignalType.CLOSE,
            alpha_id="hyper-turbo",
            signal_id=signal_id,
            position_id=result["position_id"],
            reason=reason,
            exit_price=price,
            qty=qty,
            timestamp="2026-05-22T11:00:00Z",
        ))

    final = await executor.process_close(CloseSignal(
        type=SignalType.CLOSE,
        alpha_id="hyper-turbo",
        signal_id="sig-tp3",
        position_id=result["position_id"],
        reason="TP3",
        exit_price=114.0,
        timestamp="2026-05-22T12:00:00Z",
    ))

    assert final["closed"] is True
    assert await executor.db.get_position(result["position_id"]) is None
    trades = await executor.db.get_trades_by_alpha("hyper-turbo")
    assert len(trades) == 3
    assert sum(trade["qty"] for trade in trades) == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_process_close_position_not_found(executor):
    close_signal = CloseSignal(
        type=SignalType.CLOSE,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id="nonexistent",
        reason="SIGNAL",
        exit_price=96000.0,
        timestamp="2026-05-22T11:00:00Z",
    )
    with pytest.raises(ValueError, match="Position not found"):
        await executor.process_close(close_signal)


@pytest.mark.asyncio
async def test_check_tpsl_hit_tp_long(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN, alpha_id="test-alpha", signal_id="sig-001",
        symbol="BTCUSDT", side="LONG", entry=95000.0, qty=0.01,
        tp=97000.0, sl=94000.0, leverage=10, timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    hits = await executor.check_tpsl_hits({"BTCUSDT": 97500.0})
    assert len(hits) == 1
    assert hits[0]["position_id"] == result["position_id"]
    assert hits[0]["reason"] == "TP_HIT"


@pytest.mark.asyncio
async def test_check_tpsl_hit_single_symbol_wrapper(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN, alpha_id="test-alpha", signal_id="sig-001",
        symbol="BTCUSDT", side="LONG", entry=95000.0, qty=0.01,
        tp=97000.0, sl=94000.0, leverage=10, timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    hits = await executor.check_tpsl_hit("BTCUSDT", 97500.0)
    assert len(hits) == 1
    assert hits[0]["position_id"] == result["position_id"]


@pytest.mark.asyncio
async def test_check_tpsl_hit_sl_short(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN, alpha_id="test-alpha", signal_id="sig-001",
        symbol="BTCUSDT", side="SHORT", entry=95000.0, qty=0.01,
        tp=94000.0, sl=96000.0, leverage=10, timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    hits = await executor.check_tpsl_hits({"BTCUSDT": 96500.0})
    assert len(hits) == 1
    assert hits[0]["reason"] == "SL_HIT"


@pytest.mark.asyncio
async def test_check_tpsl_no_hit(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN, alpha_id="test-alpha", signal_id="sig-001",
        symbol="BTCUSDT", side="LONG", entry=95000.0, qty=0.01,
        tp=97000.0, sl=94000.0, timestamp="2026-05-22T10:00:00Z",
    )
    await executor.process_open(open_signal)
    hits = await executor.check_tpsl_hits({"BTCUSDT": 95500.0})
    assert len(hits) == 0


@pytest.mark.asyncio
async def test_check_tpsl_no_price_for_symbol(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN, alpha_id="test-alpha", signal_id="sig-001",
        symbol="BTCUSDT", side="LONG", entry=95000.0, qty=0.01,
        tp=97000.0, sl=94000.0, timestamp="2026-05-22T10:00:00Z",
    )
    await executor.process_open(open_signal)
    hits = await executor.check_tpsl_hits({"ETHUSDT": 3000.0})
    assert len(hits) == 0


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


@pytest.mark.asyncio
async def test_process_close_persists_close_metadata(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="WALUSDT",
        side="LONG",
        entry=0.062,
        qty=100.0,
        sl=0.061485511,
        leverage=5,
        timestamp="2026-06-01T10:00:00Z",
        metadata='{"atr": 0.0003, "poc": 0.062}',
    )
    result = await executor.process_open(open_signal)

    close_meta_in = {
        "close_model": "price_alert_side_aware",
        "reason": "SL",
        "stop_price": 0.061485511,
        "trigger_price": 0.058865,
        "raw_fill_price": 0.058865,
        "bid": 0.058865,
        "ask": 0.058875,
    }
    close_signal = CloseSignal(
        type=SignalType.CLOSE,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id=result["position_id"],
        reason="SL",
        exit_price=0.058865,
        timestamp="2026-06-01T11:00:00Z",
        metadata=json.dumps(close_meta_in),
    )
    close_result = await executor.process_close(close_signal)

    # fill_price should equal exit_price (slippage=0.0 in fixture)
    assert close_result["exit_price"] == pytest.approx(0.058865)

    trade = await executor.db.get_trade(result["position_id"])
    assert trade is not None
    meta = json.loads(trade["metadata"])
    # Open metadata keys preserved at top level
    assert meta["atr"] == pytest.approx(0.0003)
    assert meta["poc"] == pytest.approx(0.062)
    # Close metadata nested under 'close'
    assert meta["close"]["close_model"] == "price_alert_side_aware"
    assert meta["close"]["stop_price"] == pytest.approx(0.061485511)
    assert meta["close"]["trigger_price"] == pytest.approx(0.058865)
    # fill_price injected by worker (post-slippage)
    assert meta["close"]["fill_price"] == pytest.approx(0.058865)


@pytest.mark.asyncio
async def test_close_exit_price_equals_trigger_not_stop(executor):
    """LONG SL: exit_price must be bid (0.058865), not sl (0.061485511)."""
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="WALUSDT",
        side="LONG",
        entry=0.062,
        qty=100.0,
        sl=0.061485511,
        leverage=5,
        timestamp="2026-06-01T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    close_signal = CloseSignal(
        type=SignalType.CLOSE,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id=result["position_id"],
        reason="SL",
        exit_price=0.058865,  # trigger_price (bid), NOT sl level
        timestamp="2026-06-01T11:00:00Z",
    )
    await executor.process_close(close_signal)
    trade = await executor.db.get_trade(result["position_id"])
    assert trade["exit_price"] == pytest.approx(0.058865)
    assert trade["exit_price"] != pytest.approx(0.061485511)


@pytest.mark.asyncio
async def test_check_tpsl_fills_at_market_price_not_stop_level(executor):
    """Worker auto TP/SL should fill at current market price, not sl/tp level."""
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        tp=97000.0,
        sl=94000.0,
        leverage=10,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    # Market price jumped through SL (market at 93000, below sl=94000)
    hits = await executor.check_tpsl_hits({"BTCUSDT": 93000.0})
    assert len(hits) == 1
    assert hits[0]["reason"] == "SL_HIT"
    # Fill should be at market price (93000), not stop level (94000)
    assert hits[0]["exit_price"] == pytest.approx(93000.0)

    trade = await executor.db.get_trade(result["position_id"])
    meta = json.loads(trade["metadata"])
    assert meta["close"]["close_model"] == "worker_tpsl_auto"
    assert meta["close"]["stop_price"] == pytest.approx(94000.0)
    assert meta["close"]["trigger_price"] == pytest.approx(93000.0)


@pytest.mark.asyncio
async def test_check_tpsl_short_sl_fills_at_market_price(executor):
    """SHORT SL: fill at market ask price, not sl level."""
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="SHORT",
        entry=95000.0,
        qty=0.01,
        tp=94000.0,
        sl=96000.0,
        leverage=10,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)
    # Market blew through SL (97000 > 96000)
    hits = await executor.check_tpsl_hits({"BTCUSDT": 97000.0})
    assert len(hits) == 1
    assert hits[0]["reason"] == "SL_HIT"
    assert hits[0]["exit_price"] == pytest.approx(97000.0)


@pytest.mark.asyncio
async def test_full_lifecycle(executor):
    open_signal = OpenSignal(
        type=SignalType.OPEN,
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry=95000.0,
        qty=0.01,
        sl=94000.0,
        leverage=10,
        timestamp="2026-05-22T10:00:00Z",
    )
    result = await executor.process_open(open_signal)

    modify_signal = ModifySignal(
        type=SignalType.MODIFY,
        alpha_id="test-alpha",
        signal_id="sig-002",
        position_id=result["position_id"],
        sl=94500.0,
        timestamp="2026-05-22T10:30:00Z",
    )
    await executor.process_modify(modify_signal)

    close_signal = CloseSignal(
        type=SignalType.CLOSE,
        alpha_id="test-alpha",
        signal_id="sig-003",
        position_id=result["position_id"],
        reason="TP_HIT",
        exit_price=97000.0,
        timestamp="2026-05-22T11:00:00Z",
    )
    await executor.process_close(close_signal)

    trade = await executor.db.get_trade(result["position_id"])
    assert trade["pnl"] == pytest.approx(20.0)
    assert trade["sl"] == 94500.0
    assert trade["reason"] == "TP_HIT"
