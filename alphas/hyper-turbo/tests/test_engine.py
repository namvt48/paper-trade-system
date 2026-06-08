from app.engine import HyperTurboEngine
from app.strategy import HyperTurboSignal


def make_signal(**overrides):
    values = {
        "recommend": None,
        "go_long": False,
        "go_short": False,
        "tp_long_signal": False,
        "tp_short_signal": False,
        "trend": 1,
        "close": 100.0,
        "basis": 100.0,
        "dev": 1.0,
        "upper": 101.0,
        "lower": 99.0,
        "upper_tp": 102.5,
        "lower_tp": 97.5,
    }
    values.update(overrides)
    return HyperTurboSignal(**values)


def test_tp_state_machine_closes_75_then_12_5_then_remainder(monkeypatch):
    engine = HyperTurboEngine()
    engine.runtime_state = "LIVE"
    pushed = []
    monkeypatch.setattr(engine, "push_signal", lambda signal_type, **kwargs: pushed.append((signal_type, kwargs)))
    monkeypatch.setattr(engine, "mark_positions_changed", lambda: None)

    engine._apply_signal(make_signal(recommend="LONG", go_long=True), 1, 100.0)
    pos = engine._open_positions["BTCUSDT"]
    initial_qty = pos["initial_qty"]

    engine._apply_signal(make_signal(tp_long_signal=True), 2, 110.0)
    assert pos["remaining_qty"] == initial_qty * 0.25
    assert pos["be_active"] is True

    engine._apply_signal(make_signal(tp_long_signal=True), 3, 112.0)
    assert pos["remaining_qty"] == initial_qty * 0.125

    engine._apply_signal(make_signal(tp_long_signal=True), 4, 114.0)
    assert "BTCUSDT" not in engine._open_positions

    close_signals = [kwargs for signal_type, kwargs in pushed if signal_type == "CLOSE"]
    assert close_signals[0]["qty"] == initial_qty * 0.75
    assert close_signals[1]["qty"] == initial_qty * 0.125
    assert "qty" not in close_signals[2]


def test_price_alert_closes_be_position(monkeypatch):
    engine = HyperTurboEngine()
    pushed = []
    monkeypatch.setattr(engine, "push_signal", lambda signal_type, **kwargs: pushed.append((signal_type, kwargs)))
    monkeypatch.setattr(engine, "mark_positions_changed", lambda: None)
    engine._open_positions["BTCUSDT"] = {
        "position_id": "pos-1",
        "side": "LONG",
        "entry": 100.0,
        "initial_qty": 1.0,
        "remaining_qty": 0.25,
        "tp_hits": 1,
        "be_active": True,
        "last_tp_signal_bar_time": 2,
    }

    engine.on_price_alert_message({"symbol": "BTCUSDT", "bid": 99.9, "ask": 100.0})

    assert "BTCUSDT" not in engine._open_positions
    assert pushed[-1][0] == "CLOSE"
    assert pushed[-1][1]["reason"] == "BE"
