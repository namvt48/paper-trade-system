import pytest
from app.models import (
    parse_signal,
    OpenSignal,
    ModifySignal,
    CloseSignal,
    SignalType,
    RegisterColumnsSignal,
)


def test_parse_open_signal(sample_open_signal):
    signal = parse_signal(sample_open_signal)
    assert isinstance(signal, OpenSignal)
    assert signal.alpha_id == "test-alpha"
    assert signal.signal_id == "sig-001"
    assert signal.symbol == "BTCUSDT"
    assert signal.side == "LONG"
    assert signal.entry == 95000.0
    assert signal.qty == 0.01
    assert signal.tp == 97000.0
    assert signal.sl == 94000.0
    assert signal.leverage == 10


def test_parse_open_signal_optional_fields():
    data = {
        "type": "OPEN",
        "alpha_id": "alpha-1",
        "signal_id": "sig-001",
        "symbol": "BTCUSDT",
        "side": "SHORT",
        "entry": "95000.0",
        "qty": "0.01",
        "timestamp": "2026-05-22T10:00:00Z",
    }
    signal = parse_signal(data)
    assert isinstance(signal, OpenSignal)
    assert signal.tp is None
    assert signal.sl is None
    assert signal.leverage == 1


def test_parse_open_signal_accepts_float_leverage_string():
    # xau_m30 alphas emit leverage as "10.0"; int("10.0") used to raise
    # ValueError and drop the signal before execution (worker parse error).
    data = {
        "type": "OPEN",
        "alpha_id": "xau-m30-alpha-10",
        "signal_id": "sig-float-lev",
        "symbol": "XAUUSDT",
        "side": "LONG",
        "entry": "4075.72",
        "qty": "2.618",
        "tp": "4099.59",
        "sl": "4066.17",
        "leverage": "10.0",
        "metadata": "{}",
        "timestamp": "2026-08-03T06:00:00Z",
    }
    signal = parse_signal(data)
    assert isinstance(signal, OpenSignal)
    assert signal.leverage == 10


def test_parse_modify_signal(sample_modify_signal):
    signal = parse_signal(sample_modify_signal)
    assert isinstance(signal, ModifySignal)
    assert signal.tp == 96000.0
    assert signal.sl == 94500.0


def test_parse_close_signal(sample_close_signal):
    signal = parse_signal(sample_close_signal)
    assert isinstance(signal, CloseSignal)
    assert signal.reason == "SIGNAL"
    assert signal.exit_price == 96000.0
    assert signal.qty is None


def test_parse_partial_close_signal(sample_close_signal):
    sample_close_signal["qty"] = "0.0025"
    signal = parse_signal(sample_close_signal)
    assert signal.qty == pytest.approx(0.0025)


def test_parse_close_signal_no_exit_price():
    data = {
        "type": "CLOSE",
        "alpha_id": "test-alpha",
        "signal_id": "sig-003",
        "position_id": "pos-001",
        "reason": "TP_HIT",
        "timestamp": "2026-05-22T11:00:00Z",
    }
    signal = parse_signal(data)
    assert isinstance(signal, CloseSignal)
    assert signal.exit_price is None


def test_parse_unknown_type():
    with pytest.raises(ValueError, match="Unknown signal type"):
        parse_signal({"type": "UNKNOWN", "alpha_id": "x"})


def _open_payload(**overrides):
    data = {
        "type": "OPEN",
        "alpha_id": "cross-15m",
        "signal_id": "sig-nan-001",
        "symbol": "ZORAUSDT",
        "side": "LONG",
        "entry": "0.05",
        "qty": "0.5",
        "timestamp": "2026-09-10T00:00:00Z",
    }
    data.update(overrides)
    return data


def test_parse_open_signal_rejects_nan_qty():
    # Regression: NaN is truthy, so `_to_float(...) or 0.0` let "nan" through;
    # SQLite stored it as NULL and the equity collector died every tick.
    with pytest.raises(ValueError, match="non-finite qty"):
        parse_signal(_open_payload(qty="nan"))


def test_parse_open_signal_rejects_inf_qty():
    with pytest.raises(ValueError, match="non-finite qty"):
        parse_signal(_open_payload(qty="inf"))


def test_parse_open_signal_rejects_nan_entry():
    with pytest.raises(ValueError, match="non-finite entry"):
        parse_signal(_open_payload(entry="nan"))


def test_parse_close_signal_rejects_nan_qty(sample_close_signal):
    sample_close_signal["qty"] = "nan"
    with pytest.raises(ValueError, match="non-finite qty"):
        parse_signal(sample_close_signal)


def test_parse_close_signal_missing_qty_stays_none(sample_close_signal):
    sample_close_signal.pop("qty", None)
    signal = parse_signal(sample_close_signal)
    assert isinstance(signal, CloseSignal)
    assert signal.qty is None


def test_parse_register_columns_signal():
    data = {
        "type": "REGISTER_COLUMNS",
        "alpha_id": "alpha-1-v5b",
        "signal_id": "sig-reg-001",
        "columns": '[{"key": "atr", "label": "ATR", "type": "number", "decimals": 6}]',
    }
    signal = parse_signal(data)
    assert isinstance(signal, RegisterColumnsSignal)
    assert signal.alpha_id == "alpha-1-v5b"
    assert (
        signal.columns
        == '[{"key": "atr", "label": "ATR", "type": "number", "decimals": 6}]'
    )
