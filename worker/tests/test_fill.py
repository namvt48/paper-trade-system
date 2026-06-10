import pytest

from app.fill import fixed_pct_fill, resolve_fill_price


def test_fixed_pct_fill_long_open_adds():
    # entry 95000, slippage_pct 0.1 -> slip 9.5 -> 95009.5
    assert fixed_pct_fill(95000.0, "LONG", 0.1, is_close=False) == 95009.5


def test_fixed_pct_fill_long_close_subtracts():
    assert fixed_pct_fill(95000.0, "LONG", 0.1, is_close=True) == 94990.5


def test_fixed_pct_fill_short_open_subtracts():
    assert fixed_pct_fill(95000.0, "SHORT", 0.1, is_close=False) == 94990.5


def test_resolve_uses_avg_when_fully_filled():
    resp = {"fallback_used": False, "filled_qty": 1.0, "requested_qty": 1.0, "avg_exec_price": 101.0}
    assert resolve_fill_price(resp, 100.0, "LONG", False, 0.5) == 101.0


def test_resolve_falls_back_on_none():
    # ref 100, LONG open, pct 0.5 -> slip 0.05 -> 100.05
    assert resolve_fill_price(None, 100.0, "LONG", False, 0.5) == 100.05


def test_resolve_falls_back_on_flag():
    resp = {"fallback_used": True}
    assert resolve_fill_price(resp, 100.0, "LONG", False, 0.5) == 100.05


def test_resolve_blends_partial_fill():
    # filled 1 @ 100 (avg), remainder 1 @ fixed (LONG open pct 0 -> 200 ref)
    resp = {"fallback_used": False, "filled_qty": 1.0, "requested_qty": 2.0, "avg_exec_price": 100.0}
    # ref 200, pct 0 -> fixed=200 ; blend = (1*100 + 1*200)/2 = 150
    assert resolve_fill_price(resp, 200.0, "LONG", False, 0.0) == 150.0


def test_resolve_executable_ref_skips_extra_slippage_on_fallback():
    # ref already executable-side (book best_bid): fallback must NOT add fixed-pct on top.
    assert resolve_fill_price(None, 100.0, "LONG", True, 0.5, ref_is_executable=True) == 100.0
    # non-executable ref (default): fixed-pct still applies (LONG close 100 -> 99.95).
    assert resolve_fill_price(None, 100.0, "LONG", True, 0.5) == pytest.approx(99.95)
