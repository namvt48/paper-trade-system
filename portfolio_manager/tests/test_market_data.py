from __future__ import annotations

import json
import time

from portfolio_manager.core.market_data import fetch_closes


class FakeMdsRedis:
    def __init__(self, candles_by_key: dict[str, list[dict]]) -> None:
        self._candles_by_key = candles_by_key

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        candles = self._candles_by_key.get(key, [])
        # kline_snapshot_v2 is newest-first (LPUSH); SnapshotReader re-sorts.
        return [json.dumps(candle) for candle in reversed(candles)][start : end + 1]


def _candles(n: int, tf_sec: int, start_close: float, step: float) -> list[dict]:
    now_ms = int(time.time() * 1000)
    out = []
    for i in range(n):
        open_time = now_ms - (n - 1 - i) * tf_sec * 1000
        out.append({"open_time": open_time, "close": start_close + i * step})
    return out


def test_fetch_closes_returns_oldest_to_newest():
    candles = _candles(5, tf_sec=3600, start_close=100.0, step=1.0)
    redis_client = FakeMdsRedis({"kline_snapshot_v2:binance:1h:BTCUSDT": candles})

    closes = fetch_closes(redis_client, "binance", "BTCUSDT", "1h", bars=5)

    assert closes == [100.0, 101.0, 102.0, 103.0, 104.0]


def test_fetch_closes_returns_none_when_missing():
    redis_client = FakeMdsRedis({})

    assert fetch_closes(redis_client, "binance", "BTCUSDT", "1h", bars=5) is None


def test_fetch_closes_returns_none_when_stale():
    # Distinct, ascending open_time values (so dedup keeps all 5 rows) that
    # are all far in the past, so the reader's own staleness gate rejects
    # the read instead of "too few rows after dedup".
    stale = [{"open_time": i * 3_600_000, "close": 100.0} for i in range(5)]
    redis_client = FakeMdsRedis({"kline_snapshot_v2:binance:1h:BTCUSDT": stale})

    assert fetch_closes(redis_client, "binance", "BTCUSDT", "1h", bars=5) is None
