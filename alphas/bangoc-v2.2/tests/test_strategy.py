from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

from base.models import SymbolData


ALPHA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ALPHA_DIR))

strategy = importlib.import_module("app.strategy")
engine = importlib.import_module("app.engine")
base_engine = importlib.import_module("base.engine").BaseEngine


def test_engine_uses_the_legacy_runner_base_engine_class() -> None:
    assert issubclass(engine.BangocV22Engine, base_engine)


def _indicators(side: str) -> object:
    return strategy.BangocIndicators(
        side=side,
        close=100.0,
        indi1_green=side == "LONG",
        indi1_acol=0.2,
        indi1_acol_prev=0.1,
        indi2_green=side == "LONG",
        indi2_poc=100.0,
        indi2_lower=90.0,
        indi2_upper=110.0,
    )


def _configured_engine(m15_open_ms: int, h1_open_ms: int):
    instance = engine.BangocV22Engine()
    instance.runtime_state = "LIVE"
    instance.symbol_data = {
        "BTCUSDT": {
            "15m": SymbolData(price_list=[100.0], time_list=[m15_open_ms]),
            "1h": SymbolData(price_list=[100.0], time_list=[h1_open_ms]),
        }
    }
    captured: list[dict[str, object]] = []

    def capture(signal_type: str, **fields: object) -> None:
        captured.append({"type": signal_type, **fields})

    instance.push_signal = capture
    return instance, captured


def test_engine_waits_for_current_h1_then_processes_the_same_m15_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "compute_bangoc_indicators", lambda _: _indicators("LONG"))
    monkeypatch.setattr(engine, "compute_bangoc_dot_color", lambda _: True)
    instance, captured = _configured_engine(3_600_000, 0)

    asyncio.run(instance._process_symbol())
    assert captured == []

    instance.symbol_data["BTCUSDT"]["1h"].time_list = [3_600_000]
    asyncio.run(instance._process_symbol())
    assert [signal["type"] for signal in captured] == ["OPEN"]


def test_engine_keeps_existing_position_when_h1_dot_rejects_m15_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "compute_bangoc_indicators", lambda _: _indicators("SHORT"))
    monkeypatch.setattr(engine, "compute_bangoc_dot_color", lambda _: True)
    instance, captured = _configured_engine(3_600_000, 3_600_000)
    original_position = {"position_id": "open-long", "side": "LONG", "entry": 100.0}
    instance._open_positions = {"BTCUSDT": original_position.copy()}

    asyncio.run(instance._process_symbol())

    assert captured == []
    assert instance._open_positions == {"BTCUSDT": original_position}


def test_engine_blocks_new_entries_when_runner_state_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "compute_bangoc_indicators", lambda _: _indicators("LONG"))
    monkeypatch.setattr(engine, "compute_bangoc_dot_color", lambda _: True)
    instance, captured = _configured_engine(3_600_000, 3_600_000)
    instance.set_runner_entry_gate(lambda: False)

    asyncio.run(instance._process_symbol())

    assert captured == []
    assert instance._open_positions == {}


def test_h1_dot_color_uses_only_indi1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy, "_compute_indi1", lambda **_: (True, 0.2, 0.1))

    def unexpected_indi2(*_: object) -> None:
        raise AssertionError("H1 dot gate must not read indi2")

    monkeypatch.setattr(strategy, "_compute_indi2", unexpected_indi2)

    assert strategy.compute_bangoc_dot_color([100.0] * 600) is True


@pytest.mark.parametrize(
    ("m15_side", "h1_dot_green", "expected"),
    [
        ("LONG", True, True),
        ("SHORT", False, True),
        ("LONG", False, False),
        ("SHORT", True, False),
        ("LONG", None, False),
    ],
)
def test_m15_signal_requires_matching_h1_dot(
    m15_side: str,
    h1_dot_green: bool | None,
    expected: bool,
) -> None:
    assert strategy.is_m15_signal_allowed_by_h1_dot(m15_side, h1_dot_green) is expected
