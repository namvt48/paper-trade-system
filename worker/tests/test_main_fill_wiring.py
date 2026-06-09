import pytest

from app.db import Database
from app.executor import Executor
from app.main import process_signal_message


class _FillService:
    def __init__(self, price):
        self.price = price
        self.calls = []

    async def resolve(self, exchange, symbol, position_side, qty, ref_price, is_close, request_id=None):
        self.calls.append((symbol, position_side, is_close))
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
async def test_open_signal_without_fill_service_is_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.1)
    data = {"type": "OPEN", "alpha_id": "a", "signal_id": "s2", "symbol": "BTCUSDT",
            "side": "LONG", "entry": "95000.0", "qty": "0.01", "timestamp": "2026-05-22T10:00:00Z"}
    result = await process_signal_message(data, db, ex)  # no fill_service
    assert result["fill_price"] == pytest.approx(95009.5)
    await db.close()
