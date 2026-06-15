import pytest
import json
from unittest.mock import AsyncMock
from redis.exceptions import ResponseError
from app.db import Database
from app.executor import Executor
from app.main import ensure_consumer_group, process_signal_message


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
async def test_process_signal_message_commits_once_per_signal(setup, monkeypatch):
    db, executor = setup
    commit_count = 0
    original_commit = db._conn.commit

    async def counted_commit():
        nonlocal commit_count
        commit_count += 1
        await original_commit()

    monkeypatch.setattr(db._conn, "commit", counted_commit)
    data = {
        "type": "OPEN",
        "alpha_id": "test-alpha",
        "signal_id": "sig-commit",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "95000.0",
        "qty": "0.01",
        "timestamp": "2026-05-22T10:00:00Z",
    }
    await process_signal_message(data, db, executor)
    assert commit_count == 1


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
async def test_process_signal_message_close_with_metadata(setup):
    db, executor = setup
    import json
    open_data = {
        "type": "OPEN",
        "alpha_id": "test-alpha",
        "signal_id": "sig-001",
        "symbol": "WALUSDT",
        "side": "LONG",
        "entry": "0.062",
        "qty": "100.0",
        "sl": "0.061485511",
        "leverage": "5",
        "metadata": '{"atr": 0.0003, "poc": 0.062}',
        "timestamp": "2026-06-01T10:00:00Z",
    }
    result = await process_signal_message(open_data, db, executor)

    close_meta = json.dumps({
        "close_model": "price_alert_side_aware",
        "reason": "SL",
        "stop_price": 0.061485511,
        "trigger_price": 0.058865,
        "raw_fill_price": 0.058865,
    })
    close_data = {
        "type": "CLOSE",
        "alpha_id": "test-alpha",
        "signal_id": "sig-002",
        "position_id": result["position_id"],
        "reason": "SL",
        "exit_price": "0.058865",
        "metadata": close_meta,
        "timestamp": "2026-06-01T11:00:00Z",
    }
    await process_signal_message(close_data, db, executor)

    trade = await db.get_trade(result["position_id"])
    assert trade is not None
    meta = json.loads(trade["metadata"])
    assert meta["atr"] == pytest.approx(0.0003)
    assert meta["close"]["stop_price"] == pytest.approx(0.061485511)
    assert meta["close"]["trigger_price"] == pytest.approx(0.058865)
    assert "fill_price" in meta["close"]


@pytest.mark.asyncio
async def test_worker_tpsl_disabled_by_default(setup):
    """When ENABLE_WORKER_TPSL_AUTO_CLOSE is False, no ticker subscription is created."""
    from app.config import settings
    assert settings.ENABLE_WORKER_TPSL_AUTO_CLOSE is False


@pytest.mark.asyncio
async def test_ensure_consumer_group_ignores_busygroup():
    redis = AsyncMock()
    redis.xgroup_create.side_effect = ResponseError("BUSYGROUP Consumer Group name already exists")

    await ensure_consumer_group(redis)


@pytest.mark.asyncio
async def test_ensure_consumer_group_surfaces_other_redis_errors():
    redis = AsyncMock()
    redis.xgroup_create.side_effect = ResponseError("NOAUTH Authentication required")

    with pytest.raises(ResponseError, match="NOAUTH"):
        await ensure_consumer_group(redis)


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
