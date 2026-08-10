from __future__ import annotations

from types import SimpleNamespace

from runner.strategies.xau_m30.strategy import M15_MS, XauM30RunnerStrategy


class _Cache:
    def snapshot(self, symbol: str, tf: str, bars: int):
        assert symbol == "XAUUSDT"
        assert tf == "15m"
        # Unix SECONDS (0, 15m, 30m) -- the strategy's _timestamp_ms normalizes
        # values < 1e12 to milliseconds, so these become (0, 900000, 1800000).
        return SimpleNamespace(
            times=(0, 900, 1800),
            opens=(100.0, 102.0, 104.0),
            highs=(103.0, 105.0, 106.0),
            lows=(99.0, 101.0, 103.0),
            closes=(102.0, 104.0, 105.0),
        )


def test_m30_builder_uses_only_complete_adjacent_m15_pairs() -> None:
    strategy = object.__new__(XauM30RunnerStrategy)
    strategy.symbol = "XAUUSDT"
    strategy.ctx = SimpleNamespace(cache=_Cache())
    strategy.timestamp_semantics = "open"
    strategy.get_retain_bars = lambda tf: 10

    series = strategy._m30_series()

    assert series.times == (0,)
    assert series.opens == (100.0,)
    assert series.highs == (105.0,)
    assert series.lows == (99.0,)
    assert series.closes == (104.0,)


def test_xau_strategy_requests_only_available_mds_timeframes() -> None:
    assert XauM30RunnerStrategy.get_required_channels({"preset": 4}) == [
        "kline:15m",
        "kline:4h",
    ]


def test_xau_strategy_skips_4h_for_non_macro_gated_preset() -> None:
    # Preset 10 has macro_gated=False (no H4 filter) -- the new logic must
    # NOT request the 4h channel, matching its warmup/timeframe footprint.
    assert XauM30RunnerStrategy.get_required_channels({"preset": 10}) == ["kline:15m"]
