import pytest

from app.config import BACKTEST_SYMBOLS, settings
from app.engine import HyperTurboV2Engine
from app.strategy import HyperTurboSignal


def make_signal(**overrides):
    values = {
        "recommend": "LONG",
        "go_long": True,
        "go_short": False,
        "period_trends": (1, 1, 1),
        "period_votes": (1, 1, 1),
        "close": 105.0,
        "basis": 100.0,
        "dev": 2.0,
        "upper": 102.0,
        "lower": 98.0,
        "atr": 3.0,
        "risk_atr": 2.0,
        "htf_ma": 95.0,
        "atr_rising": True,
        "htf_pass": True,
    }
    values.update(overrides)
    return HyperTurboSignal(**values)


def ready_engine(monkeypatch):
    engine = HyperTurboV2Engine()
    engine.runtime_state = "LIVE"
    pushed = []
    monkeypatch.setattr(engine, "push_signal", lambda signal_type, **kwargs: pushed.append((signal_type, kwargs)))
    monkeypatch.setattr(engine, "mark_positions_changed", lambda: None)
    return engine, pushed


def test_engine_runs_only_the_19_backtest_symbols(monkeypatch):
    monkeypatch.setattr(settings, "SYMBOL_BLACKLIST", "BTCUSDT,CAKEUSDT")
    engine = HyperTurboV2Engine()

    assert len(BACKTEST_SYMBOLS) == 19
    assert tuple(engine._symbols) == BACKTEST_SYMBOLS
    assert settings.MAX_CONCURRENT_POSITIONS == len(BACKTEST_SYMBOLS)


def test_open_sizes_from_previous_atr_and_applies_entry_slippage(monkeypatch):
    engine, pushed = ready_engine(monkeypatch)

    engine._apply_signal("BTCUSDT", make_signal(), 1, 2, 100.0)

    pos = engine._open_positions["BTCUSDT"]
    expected_entry = 100.0 * (1 + settings.SLIPPAGE_PCT)
    expected_stop_distance = settings.ATR_STOP_MULTIPLIER * 2.0
    expected_risk_qty = settings.CAPITAL * settings.RISK_PER_TRADE / expected_stop_distance
    assert pos["entry"] == pytest.approx(expected_entry)
    assert pos["qty"] == pytest.approx(expected_risk_qty)
    assert pos["sl"] == pytest.approx(expected_entry - expected_stop_distance)
    assert pushed[0][0] == "OPEN"


def test_reverse_closes_then_opens_opposite_position(monkeypatch):
    engine, pushed = ready_engine(monkeypatch)
    engine._apply_signal("BTCUSDT", make_signal(), 1, 2, 100.0)

    short = make_signal(recommend="SHORT", go_long=False, go_short=True, period_votes=(-1, -1, -1))
    engine._apply_signal("BTCUSDT", short, 2, 3, 110.0)

    assert engine._open_positions["BTCUSDT"]["side"] == "SHORT"
    assert [kind for kind, _ in pushed] == ["OPEN", "MODIFY", "CLOSE", "OPEN"]
    assert pushed[2][1]["reason"] == "REVERSE"


def test_trailing_stop_is_monotonic_and_price_alert_closes(monkeypatch):
    engine, pushed = ready_engine(monkeypatch)
    engine._apply_signal("BTCUSDT", make_signal(), 1, 2, 100.0)
    old_stop = engine._open_positions["BTCUSDT"]["sl"]

    no_entry = make_signal(recommend=None, go_long=False, close=120.0, atr=2.0, period_votes=(0, 0, 0))
    engine._apply_signal("BTCUSDT", no_entry, 2, 3, 120.0)
    new_stop = engine._open_positions["BTCUSDT"]["sl"]
    assert new_stop > old_stop

    engine.on_price_alert_message({"symbol": "BTCUSDT", "bid": new_stop - 0.01})

    assert "BTCUSDT" not in engine._open_positions
    assert pushed[-1][0] == "CLOSE"
    assert pushed[-1][1]["reason"] == "ATR_TRAILING_STOP"


def test_stale_execution_candle_does_not_open(monkeypatch):
    engine, pushed = ready_engine(monkeypatch)

    engine._apply_signal("BTCUSDT", make_signal(), 1, 2, 100.0, allow_entry=False)

    assert "BTCUSDT" not in engine._open_positions
    assert pushed == []


def test_exit_price_includes_slippage_and_funding_cost():
    pos = {"side": "LONG", "entry": 100.0, "opened_at_ms": 0}

    exit_price = HyperTurboV2Engine._cost_adjusted_exit(pos, 110.0, close_time_ms=8 * 3_600_000)

    expected = 110.0 * (1 - settings.SLIPPAGE_PCT) - 100.0 * settings.FUNDING_RATE_8H
    assert exit_price == pytest.approx(expected)


def test_reverse_signal_closes_without_reopening_when_gate_fails(monkeypatch):
    engine, pushed = ready_engine(monkeypatch)
    engine._apply_signal("BTCUSDT", make_signal(), 1, 2, 100.0)
    blocked_short = make_signal(
        recommend=None,
        go_long=False,
        go_short=True,
        period_votes=(-1, -1, -1),
        htf_pass=False,
    )

    engine._apply_signal("BTCUSDT", blocked_short, 2, 3, 110.0)

    assert "BTCUSDT" not in engine._open_positions
    assert pushed[-1][0] == "CLOSE"
    assert pushed[-1][1]["reason"] == "REVERSE"


def test_gap_through_new_trailing_stop_takes_priority_over_reverse(monkeypatch):
    engine, pushed = ready_engine(monkeypatch)
    engine._apply_signal("BTCUSDT", make_signal(), 1, 2, 100.0)
    reverse = make_signal(recommend="SHORT", go_long=False, go_short=True, close=120.0, atr=2.0)

    engine._apply_signal("BTCUSDT", reverse, 2, 3, 100.0, market_price=100.0, allow_entry=False)

    assert "BTCUSDT" not in engine._open_positions
    assert pushed[-1][1]["reason"] == "ATR_TRAILING_STOP"


def test_close_updates_equity_used_by_next_position(monkeypatch):
    engine, _ = ready_engine(monkeypatch)
    engine._apply_signal("BTCUSDT", make_signal(), 1, 2, 100.0)
    first_qty = engine._open_positions["BTCUSDT"]["qty"]
    pos = engine._open_positions["BTCUSDT"]
    engine._close_position("BTCUSDT", pos, 110.0, "TEST")

    engine._apply_signal("ETHUSDT", make_signal(), 2, 3, 100.0)

    assert engine._equity > settings.CAPITAL
    assert engine._open_positions["ETHUSDT"]["qty"] > first_qty
