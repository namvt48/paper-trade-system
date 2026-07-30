from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import fakeredis
import pytest

ALPHA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ALPHA_DIR))
sys.path.insert(0, str(ALPHA_DIR.parent))

from base.models import SymbolData  # noqa: E402

config_module = importlib.import_module("app.config")
engine_module = importlib.import_module("app.engine")

T0 = 1_700_000_000_000
BAR_MS = 900_000
DAY_MS = 86_400_000
SYMBOL = "BTCUSDT"


@pytest.fixture(autouse=True)
def _small_indicator_periods(monkeypatch: pytest.MonkeyPatch):
    settings = config_module.settings
    monkeypatch.setattr(settings, "EMA_FAST", 3)
    monkeypatch.setattr(settings, "EMA_SLOW", 5)
    monkeypatch.setattr(settings, "RSI_LEN", 5)
    monkeypatch.setattr(settings, "ATR_LEN", 5)
    monkeypatch.setattr(settings, "D1_GATE_LOOKBACK_HOURS", 2)  # -> lookback_bars = 8
    monkeypatch.setattr(settings, "D1_EMA_FAST", 3)
    monkeypatch.setattr(settings, "D1_EMA_SLOW", 5)
    monkeypatch.setattr(settings, "D1_SLOPE_LOOKBACK", 3)


def _downtrend_15m_symbol_data(n: int = 15) -> SymbolData:
    closes = [100.0 - i for i in range(n)]
    opens = [c + 0.3 for c in closes]
    highs = [c + 1.0 for c in closes]
    lows = [c - 0.1 for c in closes]
    times = [T0 + i * BAR_MS for i in range(n)]
    return SymbolData(price_list=closes, open_list=opens, high_list=highs, low_list=lows, time_list=times)


def _downtrend_d1_symbol_data(n: int = 15) -> SymbolData:
    closes = [200.0 - j for j in range(n)]
    times = [T0 - (20 - j) * DAY_MS for j in range(n)]
    return SymbolData(price_list=closes, time_list=times)


def _configured_engine():
    instance = engine_module.ShortBtcV1Engine()
    instance.runtime_state = "LIVE"
    instance.symbol_data = {
        SYMBOL: {
            "15m": _downtrend_15m_symbol_data(),
            "1d": _downtrend_d1_symbol_data(),
        }
    }
    captured: list[dict] = []

    def capture(signal_type: str, **fields):
        captured.append({"type": signal_type, **fields})

    instance.push_signal = capture
    return instance, captured


def test_open_reduce_then_sl_close_full_flow(monkeypatch: pytest.MonkeyPatch):
    instance, captured = _configured_engine()

    # ── Bar 14 (last of the seeded series): entry fires ────────────────────
    row = instance._build_symbol_row(SYMBOL)
    assert row is not None
    indic = instance._compute_indicators(row)
    assert indic["entry_signal"] is not None

    instance._apply_decision(row, indic)

    assert [c["type"] for c in captured] == ["OPEN"]
    pos = instance._open_positions[SYMBOL]
    assert pos["side"] == "SHORT"
    assert pos["entry"] == pytest.approx(86.0)
    entry_time_ms = pos["entry_candle_open_ms"]
    original_qty = pos["qty"]

    # ── Seed MDS funding/OI context: funding bad, OI not bad -> bad_count=1 ──
    fake_client = fakeredis.FakeStrictRedis(decode_responses=True)
    fake_client.lpush(
        "funding_snapshot:binance:BTCUSDT",
        json.dumps({"funding_time": entry_time_ms - 1_000, "funding_rate": -0.0001}),
    )
    fake_client.lpush(
        "oi_snapshot:binance:BTCUSDT:1d",
        json.dumps({"open_time": entry_time_ms - 3 * DAY_MS, "oi_close": 100.0}),
    )
    fake_client.lpush(
        "oi_snapshot:binance:BTCUSDT:1d",
        json.dumps({"open_time": entry_time_ms - 2 * DAY_MS, "oi_close": 110.0}),
    )
    monkeypatch.setattr(instance, "_mds_context_redis", lambda: fake_client)

    # ── Bar 15: price rose against the short (86.5 >= entry 86.0) but stays
    # under SL (86.88) -> triggers the reduce evaluation, not a stop-out.
    sd = instance.symbol_data[SYMBOL]["15m"]
    sd.time_list.append(T0 + 15 * BAR_MS)
    sd.open_list.append(86.3)
    sd.high_list.append(86.7)
    sd.low_list.append(86.0)
    sd.price_list.append(86.5)

    row = instance._build_symbol_row(SYMBOL)
    indic = instance._compute_indicators(row)
    assert indic["reduce_decision"] is not None
    assert indic["reduce_decision"]["reduce_fraction"] == pytest.approx(0.7)
    assert indic["reduce_decision"]["context_fields"]["context_bad_count"] == 1

    instance._apply_decision(row, indic)

    assert [c["type"] for c in captured] == ["OPEN", "CLOSE"]
    reduce_signal = captured[1]
    assert reduce_signal["reason"] == "REDUCE70"
    assert reduce_signal["qty"] == pytest.approx(original_qty * 0.7)

    pos = instance._open_positions[SYMBOL]
    assert pos["reduced"] is True
    assert pos["qty"] == pytest.approx(original_qty * 0.3)

    # ── Bar 16: SL breached (high 87.0 >= sl 86.88) -> closes the remainder ──
    sd.time_list.append(T0 + 16 * BAR_MS)
    sd.open_list.append(86.8)
    sd.high_list.append(87.0)
    sd.low_list.append(86.6)
    sd.price_list.append(86.9)

    row = instance._build_symbol_row(SYMBOL)
    indic = instance._compute_indicators(row)
    assert indic["reduce_decision"] is None  # already reduced once, no second lookup

    instance._apply_decision(row, indic)

    assert [c["type"] for c in captured] == ["OPEN", "CLOSE", "CLOSE"]
    final_close = captured[2]
    assert final_close["reason"] == "SL"
    assert SYMBOL not in instance._open_positions


def test_no_reduce_when_price_kept_falling(monkeypatch: pytest.MonkeyPatch):
    instance, captured = _configured_engine()

    row = instance._build_symbol_row(SYMBOL)
    indic = instance._compute_indicators(row)
    instance._apply_decision(row, indic)
    assert [c["type"] for c in captured] == ["OPEN"]

    fake_client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(instance, "_mds_context_redis", lambda: fake_client)

    def _fail_fetch(symbol):
        raise AssertionError("context redis should not be queried when price kept falling")

    monkeypatch.setattr(instance, "_fetch_context_rows", _fail_fetch)

    sd = instance.symbol_data[SYMBOL]["15m"]
    sd.time_list.append(T0 + 15 * BAR_MS)
    sd.open_list.append(85.5)
    sd.high_list.append(85.6)
    sd.low_list.append(85.0)
    sd.price_list.append(85.2)  # still falling: below signal_close (86.0)

    row = instance._build_symbol_row(SYMBOL)
    indic = instance._compute_indicators(row)
    assert indic["reduce_decision"] is None

    instance._apply_decision(row, indic)
    assert [c["type"] for c in captured] == ["OPEN"]
    assert instance._open_positions[SYMBOL]["reduced"] is False
