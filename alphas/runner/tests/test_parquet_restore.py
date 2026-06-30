from __future__ import annotations

import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from runner.data_layer.cache import SharedCandleCache
from runner.data_layer.parquet_restore import (
    get_latest_open_time,
    read_parquet_candles,
    restore_from_parquet,
)
from runner.data_layer.rollup import rollup_from_1m

SCHEMA = pa.schema([
    pa.field("open_time", pa.int64()),
    pa.field("close_time", pa.int64()),
    pa.field("open", pa.float64()),
    pa.field("high", pa.float64()),
    pa.field("low", pa.float64()),
    pa.field("close", pa.float64()),
    pa.field("volume", pa.float64()),
    pa.field("confirmed", pa.bool_()),
])


def _make_table(candles: list[dict]) -> pa.Table:
    col_names = ["open_time", "close_time", "open", "high", "low", "close", "volume", "confirmed"]
    arrays = []
    for name in col_names:
        arrays.append(pa.array([c[name] for c in candles]))
    return pa.Table.from_arrays(arrays, names=col_names)


def _base_candles() -> list[dict]:
    return [
        {
            "open_time": 1000 + i * 1000,
            "close_time": 1000 + i * 1000 + 999,
            "open": float(i * 10),
            "high": float(i * 10 + 5),
            "low": float(i * 10 - 2),
            "close": float(i * 10 + 1),
            "volume": float(i * 100),
            "confirmed": True,
        }
        for i in range(3)
    ]


def _delta_candles() -> list[dict]:
    return [
        {
            "open_time": 4000,
            "close_time": 4999,
            "open": 30.0,
            "high": 35.0,
            "low": 28.0,
            "close": 31.0,
            "volume": 300.0,
            "confirmed": True,
        },
        {
            "open_time": 5000,
            "close_time": 5999,
            "open": 40.0,
            "high": 45.0,
            "low": 38.0,
            "close": 41.0,
            "volume": 400.0,
            "confirmed": True,
        },
    ]


def _write_parquet(path: str, candles: list[dict]) -> None:
    table = _make_table(candles)
    pq.write_table(table, path)


def _setup_cache_dir(tmp_path, exchange: str = "binance", symbol: str = "BTCUSDT"):
    symbol_dir = os.path.join(str(tmp_path), exchange, "1m", symbol)
    os.makedirs(symbol_dir, exist_ok=True)
    return symbol_dir


def test_read_parquet_candles(tmp_path):
    symbol_dir = _setup_cache_dir(tmp_path)
    _write_parquet(os.path.join(symbol_dir, "base.parquet"), _base_candles())
    _write_parquet(os.path.join(symbol_dir, "delta_001.parquet"), _delta_candles())

    result = read_parquet_candles(str(tmp_path), "binance", "BTCUSDT")
    assert len(result) == 5
    assert result[0]["open_time"] == 1000
    assert result[-1]["open_time"] == 5000


def test_read_parquet_candles_tail_rows(tmp_path):
    symbol_dir = _setup_cache_dir(tmp_path)
    _write_parquet(os.path.join(symbol_dir, "base.parquet"), _base_candles())
    _write_parquet(os.path.join(symbol_dir, "delta_001.parquet"), _delta_candles())

    result = read_parquet_candles(str(tmp_path), "binance", "BTCUSDT", tail_rows=2)

    assert [c["open_time"] for c in result] == [4000, 5000]


def test_read_parquet_candles_deduplicates(tmp_path):
    symbol_dir = _setup_cache_dir(tmp_path)
    _write_parquet(os.path.join(symbol_dir, "base.parquet"), _base_candles())
    overlap = [
        {
            "open_time": 3000,
            "close_time": 3999,
            "open": 999.0,
            "high": 999.0,
            "low": 999.0,
            "close": 999.0,
            "volume": 999.0,
            "confirmed": True,
        },
        {
            "open_time": 4000,
            "close_time": 4999,
            "open": 30.0,
            "high": 35.0,
            "low": 28.0,
            "close": 31.0,
            "volume": 300.0,
            "confirmed": True,
        },
    ]
    _write_parquet(os.path.join(symbol_dir, "delta_001.parquet"), overlap)

    result = read_parquet_candles(str(tmp_path), "binance", "BTCUSDT")
    assert len(result) == 4
    deduped_3000 = [c for c in result if c["open_time"] == 3000][0]
    assert deduped_3000["close"] == 999.0


def test_restore_from_parquet(tmp_path):
    symbol_dir = _setup_cache_dir(tmp_path)
    _write_parquet(os.path.join(symbol_dir, "base.parquet"), _base_candles())
    _write_parquet(os.path.join(symbol_dir, "delta_001.parquet"), _delta_candles())

    cache = SharedCandleCache(data_max_candles_floor=100)
    restored = restore_from_parquet(str(tmp_path), "binance", cache)
    assert restored == 5
    assert cache.get_bar_count("BTCUSDT", "1m") == 5


def test_restore_from_parquet_filters_symbols_and_tail(tmp_path):
    btc_dir = _setup_cache_dir(tmp_path, symbol="BTCUSDT")
    eth_dir = _setup_cache_dir(tmp_path, symbol="ETHUSDT")
    _write_parquet(os.path.join(btc_dir, "base.parquet"), _base_candles())
    _write_parquet(os.path.join(btc_dir, "delta_001.parquet"), _delta_candles())
    _write_parquet(os.path.join(eth_dir, "base.parquet"), _base_candles())

    cache = SharedCandleCache()
    restored = restore_from_parquet(
        str(tmp_path),
        "binance",
        cache,
        symbols={"BTCUSDT"},
        tail_rows_by_symbol={"BTCUSDT": 2},
    )

    assert restored == 2
    assert cache.get_bar_count("BTCUSDT", "1m") == 2
    assert cache.get_bar_count("ETHUSDT", "1m") == 0


def test_restore_from_parquet_clears_unrequired_1m_after_rollup(tmp_path):
    symbol_dir = _setup_cache_dir(tmp_path)
    base_ts = 300_000
    candles = [
        {
            "open_time": base_ts + i * 60_000,
            "close_time": base_ts + i * 60_000 + 59_999,
            "open": float(i),
            "high": float(i + 1),
            "low": float(i - 0.5),
            "close": float(i + 0.5),
            "volume": 100.0,
            "confirmed": True,
        }
        for i in range(15)
    ]
    _write_parquet(os.path.join(symbol_dir, "base.parquet"), candles)

    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "5m", warmup_bars=3, retain_bars=3)
    restored = restore_from_parquet(
        str(tmp_path),
        "binance",
        cache,
        tfs_to_rollup=["5m"],
        symbols={"BTCUSDT"},
        clear_unrequired_1m_after_rollup=True,
    )

    assert restored == 3
    assert cache.get_bar_count("BTCUSDT", "5m") == 3
    assert cache.get_bar_count("BTCUSDT", "1m") == 0


def test_get_latest_open_time(tmp_path):
    symbol_dir = _setup_cache_dir(tmp_path)
    _write_parquet(os.path.join(symbol_dir, "base.parquet"), _base_candles())
    _write_parquet(os.path.join(symbol_dir, "delta_001.parquet"), _delta_candles())

    result = get_latest_open_time(str(tmp_path), "binance", "BTCUSDT")
    assert result == 5000


def test_restore_empty_dir(tmp_path):
    symbol_dir = _setup_cache_dir(tmp_path)
    restored = restore_from_parquet(str(tmp_path), "binance", SharedCandleCache())
    assert restored == 0


def test_restore_nonexistent_dir(tmp_path):
    cache = SharedCandleCache()
    restored = restore_from_parquet(str(tmp_path) + "/nope", "binance", cache)
    assert restored == 0


def test_rollup_from_1m():
    cache = SharedCandleCache(data_max_candles_floor=200)
    base_ts = 300_000
    for i in range(60):
        cache.upsert_candle("BTCUSDT", "1m", {
            "open_time": base_ts + i * 60_000,
            "close_time": base_ts + i * 60_000 + 59_999,
            "open": float(i),
            "high": float(i + 1),
            "low": float(i - 0.5),
            "close": float(i + 0.5),
            "volume": 100.0,
        })

    total = rollup_from_1m(cache, "5m", ["BTCUSDT"])
    assert total == 12
    assert cache.get_bar_count("BTCUSDT", "5m") == 12


def test_rollup_from_1m_aligns_to_tf_boundary_and_skips_partial_edges():
    cache = SharedCandleCache(data_max_candles_floor=200)
    for i in range(15):
        open_time = 60_000 + i * 60_000
        cache.upsert_candle("BTCUSDT", "1m", {
            "open_time": open_time,
            "close_time": open_time + 59_999,
            "open": float(i),
            "high": float(i + 1),
            "low": float(i - 0.5),
            "close": float(i + 0.5),
            "volume": 100.0,
        })

    total = rollup_from_1m(cache, "5m", ["BTCUSDT"])
    assert total == 2
    assert cache.get_times("BTCUSDT", "5m") == (300_000, 600_000)


def test_rollup_from_1m_skips_gapped_bucket():
    cache = SharedCandleCache(data_max_candles_floor=200)
    base_ts = 300_000
    for i in [0, 1, 2, 4, 5]:
        open_time = base_ts + i * 60_000
        cache.upsert_candle("BTCUSDT", "1m", {
            "open_time": open_time,
            "close_time": open_time + 59_999,
            "open": float(i),
            "high": float(i + 1),
            "low": float(i - 0.5),
            "close": float(i + 0.5),
            "volume": 100.0,
        })

    total = rollup_from_1m(cache, "5m", ["BTCUSDT"])
    assert total == 0
    assert cache.get_bar_count("BTCUSDT", "5m") == 0
