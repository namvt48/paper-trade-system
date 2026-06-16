from __future__ import annotations

import time

from runner.data_layer.cache import SharedCandleCache


def candle(open_time: int, close: float) -> dict:
    return {
        "open_time": open_time,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": close * 10,
    }


def test_cache_upsert_inserts_sorted_and_replaces_same_candle():
    cache = SharedCandleCache(data_max_candles_floor=10)
    cache.upsert_candle("BTCUSDT", "15m", candle(2000, 2.0))
    cache.upsert_candle("BTCUSDT", "15m", candle(1000, 1.0))
    cache.upsert_candle("BTCUSDT", "15m", candle(2000, 3.0))

    assert cache.get_times("BTCUSDT", "15m") == (1000, 2000)
    assert cache.get_closes("BTCUSDT", "15m") == (1.0, 3.0)


def test_cache_read_api_returns_tuple_and_cannot_mutate_cache():
    cache = SharedCandleCache()
    cache.upsert_candle("BTCUSDT", "15m", candle(1000, 1.0))

    closes = cache.get_closes("BTCUSDT", "15m")

    assert isinstance(closes, tuple)
    assert closes + (99.0,) == (1.0, 99.0)
    assert cache.get_closes("BTCUSDT", "15m") == (1.0,)


def test_cache_register_max_bars_and_trim_keeps_largest_requirement():
    cache = SharedCandleCache(data_max_candles_floor=2)
    cache.register_bars_requirement("BTCUSDT", "15m", 3)
    cache.register_bars_requirement("BTCUSDT", "15m", 5)
    for i in range(8):
        cache.upsert_candle("BTCUSDT", "15m", candle(i * 1000 + 1000, float(i)))

    assert cache.required_bars("BTCUSDT", "15m") == 5
    assert cache.get_bar_count("BTCUSDT", "15m") == 5
    assert cache.get_closes("BTCUSDT", "15m") == (3.0, 4.0, 5.0, 6.0, 7.0)


def test_cache_retention_is_never_smaller_than_warmup_for_raw_candles():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "15m", warmup_bars=8, retain_bars=3)
    for i in range(8):
        cache.upsert_candle("BTCUSDT", "15m", candle(i * 1000 + 1000, float(i)))

    assert cache.required_bars("BTCUSDT", "15m") == 8
    assert cache.retained_bars("BTCUSDT", "15m") == 8
    assert cache.get_bar_count("BTCUSDT", "15m") == 8
    assert cache.get_closes("BTCUSDT", "15m") == tuple(float(i) for i in range(8))


def test_cache_retention_aggregates_max_requirement_with_buffer():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "15m", warmup_bars=8, retain_bars=3)
    cache.register_data_requirement("BTCUSDT", "15m", warmup_bars=5, retain_bars=4, retain_buffer_bars=2)
    for i in range(10):
        cache.upsert_candle("BTCUSDT", "15m", candle(i * 1000 + 1000, float(i)))

    assert cache.required_bars("BTCUSDT", "15m") == 8
    assert cache.retained_bars("BTCUSDT", "15m") == 8
    assert cache.get_bar_count("BTCUSDT", "15m") == 8
    assert cache.stats()[0].trim_count == 2


def test_cache_stats_include_registered_keys_before_data_arrives():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "15m", warmup_bars=8, retain_bars=8)

    assert cache.stats()[0].__dict__ == {
        "symbol": "BTCUSDT",
        "tf": "15m",
        "loaded_bars": 0,
        "warmup_bars": 8,
        "retain_bars": 8,
        "trim_count": 0,
    }


def test_cache_coverage_returns_loaded_count_and_percentage():
    cache = SharedCandleCache()
    now_ms = int(time.time() * 1000)
    for i in range(3):
        cache.upsert_candle("BTCUSDT", "15m", candle(now_ms + i, float(i)))

    assert cache.coverage(["BTCUSDT", "ETHUSDT"], "15m", 3, 60.0) == (1, 2, 0.5)
