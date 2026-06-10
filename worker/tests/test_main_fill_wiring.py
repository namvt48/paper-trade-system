import pytest

from app.db import Database
from app.executor import Executor
from app.main import process_signal_message


class _FillService:
    def __init__(self, price):
        self.price = price
        self.calls = []

    async def resolve(self, exchange, symbol, position_side, qty, ref_price, is_close,
                      request_id=None, ref_is_executable=False):
        self.calls.append((symbol, position_side, is_close, ref_is_executable))
        return self.price


@pytest.mark.asyncio
async def test_open_signal_uses_fill_service(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.1)
    fs = _FillService(price=95222.0)
    data = {"type": "OPEN", "alpha_id": "a", "signal_id": "s1", "symbol": "BTCUSDT",
            "side": "LONG", "entry": "95000.0", "qty": "0.01", "timestamp": "2026-05-22T10:00:00Z"}
    result = await process_signal_message(data, db, ex, fill_service=fs)
    assert result["fill_price"] == 95222.0
    assert fs.calls and fs.calls[0][0] == "BTCUSDT"
    await db.close()


@pytest.mark.asyncio
async def test_close_signal_preserves_executable_reference_provenance(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.register_alpha("a")
    await db.create_position(
        position_id="p1", alpha_id="a", signal_id="open", symbol="BTCUSDT",
        side="LONG", entry_price=95000.0, qty=0.01,
        opened_at="2026-05-22T10:00:00Z",
    )
    ex = Executor(db, slippage_pct=0.1)
    fs = _FillService(price=96000.0)
    data = {
        "type": "CLOSE", "alpha_id": "a", "signal_id": "close", "position_id": "p1",
        "exit_price": "96000", "reason": "TP_HIT", "timestamp": "2026-05-22T11:00:00Z",
        "metadata": '{"ref_is_executable": true}',
    }
    await process_signal_message(data, db, ex, fill_service=fs)
    assert fs.calls[0][3] is True
    await db.close()


@pytest.mark.asyncio
async def test_open_signal_without_fill_service_is_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.1)
    data = {"type": "OPEN", "alpha_id": "a", "signal_id": "s2", "symbol": "BTCUSDT",
            "side": "LONG", "entry": "95000.0", "qty": "0.01", "timestamp": "2026-05-22T10:00:00Z"}
    result = await process_signal_message(data, db, ex)  # no fill_service
    assert result["fill_price"] == pytest.approx(95009.5)
    await db.close()


@pytest.mark.asyncio
async def test_duplicate_open_skips_pre_subscribe_and_fill(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.register_alpha("a")
    await db.create_position(
        "p1", "a", "open", "BTCUSDT", "LONG", 95000.0, 0.01,
        "2026-05-22T10:00:00Z",
    )
    ex = Executor(db)
    fs = _FillService(price=95222.0)
    pre_calls = []

    async def pre_open(signal):
        pre_calls.append(signal.symbol)
        return "became_ready"

    data = {"type": "OPEN", "alpha_id": "a", "signal_id": "duplicate",
            "symbol": "BTCUSDT", "side": "LONG", "entry": "95000", "qty": "0.01",
            "timestamp": "2026-05-22T10:01:00Z"}
    assert await process_signal_message(data, db, ex, fill_service=fs, pre_open=pre_open) is None
    assert pre_calls == []
    assert fs.calls == []
    await db.close()


@pytest.mark.asyncio
async def test_executable_close_without_fill_service_is_not_slipped_again(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.register_alpha("a")
    await db.create_position(
        position_id="p1", alpha_id="a", signal_id="open", symbol="BTCUSDT",
        side="LONG", entry_price=95000.0, qty=0.01,
        opened_at="2026-05-22T10:00:00Z",
    )
    ex = Executor(db, slippage_pct=0.5)
    data = {
        "type": "CLOSE", "alpha_id": "a", "signal_id": "close", "position_id": "p1",
        "exit_price": "96000", "reason": "TP_HIT", "timestamp": "2026-05-22T11:00:00Z",
        "metadata": '{"ref_is_executable": true}',
    }
    result = await process_signal_message(data, db, ex)
    assert result["exit_price"] == 96000.0
    await db.close()
