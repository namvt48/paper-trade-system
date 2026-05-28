import pytest


@pytest.fixture
def sample_open_signal():
    return {
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


@pytest.fixture
def sample_modify_signal():
    return {
        "type": "MODIFY",
        "alpha_id": "test-alpha",
        "signal_id": "sig-002",
        "position_id": "",
        "tp": "96000.0",
        "sl": "94500.0",
        "metadata": "{}",
        "timestamp": "2026-05-22T10:30:00Z",
    }


@pytest.fixture
def sample_close_signal():
    return {
        "type": "CLOSE",
        "alpha_id": "test-alpha",
        "signal_id": "sig-003",
        "position_id": "",
        "reason": "SIGNAL",
        "exit_price": "96000.0",
        "metadata": "{}",
        "timestamp": "2026-05-22T11:00:00Z",
    }
