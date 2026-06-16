from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_shadow_main():
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("shadow_worker_main", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_same_logical_key_matches_despite_different_emit_time():
    shadow = load_shadow_main()
    comparator = shadow.SignalComparator(ttl_sec=10, now_func=lambda: 1)
    prod = {
        "alpha_id": "a",
        "symbol": "BTCUSDT",
        "side": "long",
        "signal_type": "OPEN",
        "signal_candle_open_ms": "1000",
        "strategy_version": "v1",
        "emitted_at_ms": "2000",
    }
    sh = {**prod, "emitted_at_ms": "2500"}

    assert comparator.observe("production", prod) is False
    assert comparator.observe("shadow", sh) is True
    assert comparator.stats.matched == 1
    assert comparator.stats.latency_deltas_ms == [500]


def test_prod_only_and_shadow_only_expire_into_mismatch_counts():
    now = 1.0

    def now_func():
        return now

    shadow = load_shadow_main()
    comparator = shadow.SignalComparator(ttl_sec=5, now_func=now_func)
    base = {
        "alpha_id": "a",
        "symbol": "BTCUSDT",
        "side": "long",
        "signal_type": "OPEN",
        "signal_candle_open_ms": "1000",
        "strategy_version": "v1",
    }
    comparator.observe("production", base)
    comparator.observe("shadow", {**base, "symbol": "ETHUSDT"})
    now = 7.0
    comparator.expire()

    assert comparator.stats.production_only == 1
    assert comparator.stats.shadow_only == 1


def test_match_rate_uses_all_terminal_outcomes():
    now = 1.0

    def now_func():
        return now

    shadow = load_shadow_main()
    comparator = shadow.SignalComparator(ttl_sec=1, now_func=now_func)
    base = {
        "alpha_id": "a",
        "symbol": "BTCUSDT",
        "side": "long",
        "signal_type": "OPEN",
        "signal_candle_open_ms": "1000",
        "strategy_version": "v1",
    }
    comparator.observe("production", base)
    comparator.observe("shadow", base)
    comparator.observe("production", {**base, "symbol": "ETHUSDT"})
    now = 3.0
    comparator.expire()

    assert comparator.stats.matched == 1
    assert comparator.stats.production_only == 1
    assert comparator.stats.match_rate == 0.5
