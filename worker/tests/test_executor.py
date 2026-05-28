import pytest
from app.db import Database
from app.executor import Executor
from app.models import OpenSignal, ModifySignal, CloseSignal, SignalType


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
    assert trade["pnl"] == pytest.approx(1.0)
    assert trade["reason"] == "SIGNAL"


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
    assert trade["pnl"] == pytest.approx(2.0)
    assert trade["sl"] == 94500.0
    assert trade["reason"] == "TP_HIT"
