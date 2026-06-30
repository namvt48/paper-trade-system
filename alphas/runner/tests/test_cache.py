from __future__ import annotations

import time

from runner.data_layer.cache import GapReport, SharedCandleCache


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


def test_cache_tf_version_changes_on_append_insert_and_replace():
    cache = SharedCandleCache(data_max_candles_floor=10)
    assert cache.get_tf_version("15m") == 0

    cache.upsert_candle("BTCUSDT", "15m", candle(2000, 2.0))
    assert cache.get_tf_version("15m") == 1

    cache.upsert_candle("BTCUSDT", "15m", candle(1000, 1.0))
    assert cache.get_tf_version("15m") == 2

    cache.upsert_candle("BTCUSDT", "15m", candle(2000, 3.0))
    assert cache.get_tf_version("15m") == 3


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


def test_cache_numpy_ring_buffer_keeps_ordered_tail_after_wrap():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "15m", warmup_bars=3, retain_bars=3)

    for i in range(5):
        cache.upsert_candle("BTCUSDT", "15m", candle((i + 1) * 1000, float(i + 1)))

    view = cache.tail_arrays("BTCUSDT", "15m", 3)
    assert view is not None
    assert cache.get_times("BTCUSDT", "15m") == (3000, 4000, 5000)
    assert cache.get_closes("BTCUSDT", "15m") == (3.0, 4.0, 5.0)
    assert tuple(view.times.tolist()) == (3000, 4000, 5000)
    assert tuple(view.closes.tolist()) == (3.0, 4.0, 5.0)


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


def test_upsert_candle_skips_older_than_baseline():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "15m", warmup_bars=5, retain_bars=10)
    cache.set_warmup_baseline("15m", 5000)
    cache.upsert_candle("BTCUSDT", "15m", {"open_time": 4000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100, "confirmed": True})
    assert cache.get_bar_count("BTCUSDT", "15m") == 0
    cache.upsert_candle("BTCUSDT", "15m", {"open_time": 6000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100, "confirmed": True})
    assert cache.get_bar_count("BTCUSDT", "15m") == 1


def test_upsert_candle_no_baseline_allows_all():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "15m", warmup_bars=5, retain_bars=10)
    cache.upsert_candle("BTCUSDT", "15m", {"open_time": 4000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100, "confirmed": True})
    assert cache.get_bar_count("BTCUSDT", "15m") == 1


def test_verify_no_gaps_returns_clean_when_consecutive():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=5, retain_bars=10)
    for i in range(5):
        cache.upsert_candle("BTCUSDT", "1m", candle(60_000 + i * 60_000, float(i)))

    report = cache.verify_no_gaps("BTCUSDT", "1m")
    assert report.is_clean is True
    assert report.gap_count == 0
    assert report.missing_ranges == ()
    assert report.total_bars == 5


def test_verify_no_gaps_detects_single_gap():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=5, retain_bars=10)
    cache.upsert_candle("BTCUSDT", "1m", candle(60_000, 1.0))
    cache.upsert_candle("BTCUSDT", "1m", candle(120_000, 2.0))
    cache.upsert_candle("BTCUSDT", "1m", candle(240_000, 3.0))

    report = cache.verify_no_gaps("BTCUSDT", "1m")
    assert report.is_clean is False
    assert report.gap_count == 1
    assert report.missing_ranges == ((180_000, 180_000),)


def test_verify_no_gaps_detects_multi_candle_gap():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=10, retain_bars=10)
    cache.upsert_candle("BTCUSDT", "1m", candle(60_000, 1.0))
    cache.upsert_candle("BTCUSDT", "1m", candle(300_000, 2.0))

    report = cache.verify_no_gaps("BTCUSDT", "1m")
    assert report.is_clean is False
    assert report.gap_count == 1
    assert report.missing_ranges == ((120_000, 240_000),)


def test_verify_no_gaps_empty_cache_returns_clean():
    cache = SharedCandleCache()
    report = cache.verify_no_gaps("BTCUSDT", "1m")
    assert report.is_clean is True
    assert report.total_bars == 0


def test_verify_no_gaps_single_bar_returns_clean():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=5, retain_bars=10)
    cache.upsert_candle("BTCUSDT", "1m", candle(60_000, 1.0))
    report = cache.verify_no_gaps("BTCUSDT", "1m")
    assert report.is_clean is True
    assert report.total_bars == 1


def test_verify_no_gaps_multiple_disjoint_gaps():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=10, retain_bars=10)
    cache.upsert_candle("BTCUSDT", "1m", candle(60_000, 1.0))
    cache.upsert_candle("BTCUSDT", "1m", candle(120_000, 2.0))
    cache.upsert_candle("BTCUSDT", "1m", candle(300_000, 3.0))
    cache.upsert_candle("BTCUSDT", "1m", candle(360_000, 4.0))
    cache.upsert_candle("BTCUSDT", "1m", candle(480_000, 5.0))

    report = cache.verify_no_gaps("BTCUSDT", "1m")
    assert report.is_clean is False
    assert report.gap_count == 2
    assert report.missing_ranges[0] == (180_000, 240_000)
    assert report.missing_ranges[1] == (420_000, 420_000)


def test_verify_all_no_gaps_checks_all_symbols():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=3, retain_bars=5)
    cache.register_data_requirement("ETHUSDT", "1m", warmup_bars=3, retain_bars=5)
    cache.upsert_candle("BTCUSDT", "1m", candle(60_000, 1.0))
    cache.upsert_candle("BTCUSDT", "1m", candle(120_000, 2.0))
    cache.upsert_candle("ETHUSDT", "1m", candle(60_000, 1.0))
    cache.upsert_candle("ETHUSDT", "1m", candle(240_000, 2.0))

    reports = cache.verify_all_no_gaps("1m")
    by_symbol = {r.symbol: r for r in reports}
    assert by_symbol["BTCUSDT"].is_clean is True
    assert by_symbol["ETHUSDT"].is_clean is False


def test_ensure_write_capacity_caps_at_max_when_retained_bars_zero():
    cache = SharedCandleCache(data_max_candles_floor=0)
    sd = cache._get_sd("BTCUSDT", "15m", create=True)
    for i in range(200):
        cache._ensure_write_capacity("BTCUSDT", "15m", sd)
        sd.size = min(sd.size + 1, sd.capacity)
        sd.capacity = sd.capacity
    assert sd.capacity <= SharedCandleCache.MAX_CAPACITY


def test_upsert_candle_works_without_registration():
    cache = SharedCandleCache(data_max_candles_floor=0)
    for i in range(20):
        cache.upsert_candle("NEWUSDT", "15m", candle(i * 900_000 + 1_000_000, float(i)))
    assert cache.get_bar_count("NEWUSDT", "15m") == 20
    assert cache.retained_bars("NEWUSDT", "15m") == 0


def test_no_overflow_when_many_upserts_without_registration():
    cache = SharedCandleCache(data_max_candles_floor=0)
    base_time = 1_700_000_000_000
    for i in range(5000):
        cache.upsert_candle("BTCUSDT", "15m", candle(base_time + i * 900_000, float(i)))
    assert cache.get_bar_count("BTCUSDT", "15m") <= SharedCandleCache.MAX_CAPACITY
    assert cache.get_bar_count("BTCUSDT", "15m") > 0
