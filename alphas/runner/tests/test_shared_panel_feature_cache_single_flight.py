"""Regression tests for panel-build single-flight (.agents/PLAN.md U5).

Before this fix, N alphas sharing the same (tf, universe) -- e.g. all the
1d cross-sectional alphas hitting a universe refresh at once -- each
independently called ``asyncio.to_thread(self._build_panel, ...)`` for
identical underlying data, multiplying load on the runner's shared
compute thread pool exactly when it's most contended. Only the first
caller for a given cache key should actually build; the rest should
await that one build.
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from runner.data_layer.cache import SharedCandleCache
from runner.shared_panel_feature_cache import SharedPanelFeatureCache


def _fake_panel() -> dict:
    index = pd.Index([1_000, 2_000], dtype="int64")
    close = pd.DataFrame({"BTCUSDT": [1.0, 2.0]}, index=index)
    return {"close": close, "high": close, "low": close, "volume": close}


@pytest.mark.asyncio
async def test_concurrent_get_bundle_for_same_key_builds_only_once(monkeypatch):
    cache = SharedCandleCache()
    feature_cache = SharedPanelFeatureCache()

    build_calls = 0
    release = asyncio.Event()

    def slow_build_panel(cache_arg, tf, symbols, bars):
        nonlocal build_calls
        build_calls += 1
        # Simulate a slow build (e.g. a large universe) without a real
        # blocking sleep -- the point under test is call *count*, not
        # wall-clock time.
        return _fake_panel()

    monkeypatch.setattr(feature_cache, "_build_panel", slow_build_panel)

    async def fetch():
        return await feature_cache.get_bundle(cache, tf="1d", symbols=("BTCUSDT",), bars=2)

    results = await asyncio.gather(*(fetch() for _ in range(5)))

    assert build_calls == 1, (
        "5 concurrent callers for the identical (tf, universe, bars, "
        "version) key must share a single panel build, not each start "
        "their own -- this is exactly the thundering-herd load that "
        "starved the runner's compute pool on 2026-07-16"
    )
    assert all(r is results[0] for r in results)
    assert feature_cache.metrics["panel_build_total"] == 1
    assert feature_cache.metrics["panel_build_single_flight_joins_total"] == 4


@pytest.mark.asyncio
async def test_get_bundle_still_caches_across_separate_calls(monkeypatch):
    cache = SharedCandleCache()
    feature_cache = SharedPanelFeatureCache()

    build_calls = 0

    def build_panel(cache_arg, tf, symbols, bars):
        nonlocal build_calls
        build_calls += 1
        return _fake_panel()

    monkeypatch.setattr(feature_cache, "_build_panel", build_panel)

    first = await feature_cache.get_bundle(cache, tf="1d", symbols=("BTCUSDT",), bars=2)
    second = await feature_cache.get_bundle(cache, tf="1d", symbols=("BTCUSDT",), bars=2)

    assert build_calls == 1
    assert first is second
    assert feature_cache.metrics["panel_cache_hits"] == 1
