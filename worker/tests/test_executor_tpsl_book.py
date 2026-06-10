import pytest

from app.db import Database
from app.executor import Executor
from app.models import OpenSignal, SignalType


async def _open(ex, side, entry, tp=None, sl=None):
    sig = OpenSignal(type=SignalType.OPEN, alpha_id="a", signal_id="s", symbol="BTCUSDT",
                     side=side, entry=entry, qty=0.01, tp=tp, sl=sl,
                     timestamp="2026-05-22T10:00:00Z")
    return await ex.process_open(sig)


@pytest.mark.asyncio
async def test_callable_price_fn_receives_side(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.0)
    await _open(ex, "LONG", 95000.0, tp=97000.0)
    seen = {}

    def price_fn(symbol, side):
        seen["side"] = side
        return 97500.0  # above TP -> hit

    hits = await ex.check_tpsl_hits(price_fn)
    assert seen["side"] == "LONG"
    assert len(hits) == 1
    await db.close()


@pytest.mark.asyncio
async def test_fill_resolver_overrides_exit(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.0)
    await _open(ex, "LONG", 95000.0, tp=97000.0)

    async def fill_resolver(exchange, symbol, side, qty, ref_price, is_close):
        assert is_close is True
        assert side == "LONG"
        return 97777.0

    hits = await ex.check_tpsl_hits({"BTCUSDT": 97500.0}, fill_resolver=fill_resolver)
    assert hits[0]["exit_price"] == 97777.0
    await db.close()


@pytest.mark.asyncio
async def test_fill_resolver_error_falls_back_to_fixed_pct(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.0)
    await _open(ex, "LONG", 95000.0, tp=97000.0)

    async def bad_resolver(exchange, symbol, side, qty, ref_price, is_close):
        raise RuntimeError("rpc down")

    # A failing resolver must not abort the auto-close pass; the hit still closes
    # via fixed-pct (pct 0 -> trigger price).
    hits = await ex.check_tpsl_hits({"BTCUSDT": 97500.0}, fill_resolver=bad_resolver)
    assert len(hits) == 1
    assert hits[0]["exit_price"] == pytest.approx(97500.0)
    await db.close()


@pytest.mark.asyncio
async def test_legacy_dict_still_works(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    ex = Executor(db, slippage_pct=0.0)
    await _open(ex, "LONG", 95000.0, tp=97000.0)
    hits = await ex.check_tpsl_hits({"BTCUSDT": 97500.0})  # no resolver -> fixed-pct
    assert hits[0]["exit_price"] == pytest.approx(97500.0)  # pct 0
    await db.close()
