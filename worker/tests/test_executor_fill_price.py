import pytest

from app.db import Database
from app.executor import Executor
from app.models import OpenSignal, CloseSignal, SignalType


@pytest.mark.asyncio
async def test_open_uses_injected_fill_price(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.1)  # would add slippage if not overridden
    sig = OpenSignal(type=SignalType.OPEN, alpha_id="a", signal_id="s1", symbol="BTCUSDT",
                     side="LONG", entry=95000.0, qty=0.01, timestamp="2026-05-22T10:00:00Z")
    result = await ex.process_open(sig, fill_price=95123.0)
    assert result["fill_price"] == 95123.0
    await db.close()


@pytest.mark.asyncio
async def test_open_without_fill_price_keeps_fixed_pct(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.1)
    sig = OpenSignal(type=SignalType.OPEN, alpha_id="a", signal_id="s2", symbol="BTCUSDT",
                     side="LONG", entry=95000.0, qty=0.01, timestamp="2026-05-22T10:00:00Z")
    result = await ex.process_open(sig)  # no fill_price -> fixed-pct (95009.5)
    assert result["fill_price"] == pytest.approx(95009.5)
    await db.close()


@pytest.mark.asyncio
async def test_close_uses_injected_fill_price(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.0)
    open_sig = OpenSignal(type=SignalType.OPEN, alpha_id="a", signal_id="s3", symbol="BTCUSDT",
                          side="LONG", entry=95000.0, qty=0.01, timestamp="2026-05-22T10:00:00Z")
    opened = await ex.process_open(open_sig)
    close_sig = CloseSignal(type=SignalType.CLOSE, alpha_id="a", signal_id="s4",
                            position_id=opened["position_id"], reason="SIGNAL",
                            timestamp="2026-05-22T11:00:00Z", exit_price=96000.0)
    result = await ex.process_close(close_sig, fill_price=95888.0)
    assert result["exit_price"] == 95888.0
    await db.close()
