import pytest

from app.aggregator import Aggregator
from app.models import KlineCandle


@pytest.fixture
def aggregator():
    return Aggregator(timeframes=["1m", "5m", "15m", "1h"])


def _make_1m_candle(symbol: str, open_time: int, o: float, h: float, l: float, c: float, v: float) -> KlineCandle:
    return KlineCandle(
        symbol=symbol,
        tf="1m",
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        open_time=open_time,
        close_time=open_time + 59999,
        confirmed=True,
    )


def test_aggregator_stores_1m_candle(aggregator):
    candle = _make_1m_candle("BTCUSDT", 1716768000000, 67000, 67500, 66800, 67200, 100)
    results = aggregator.on_1m_close(candle)
    assert len(results) == 1
    assert results[0].tf == "1m"
    assert aggregator.get_candles("BTCUSDT", "1m") == [candle]


def test_aggregator_5m_rollup(aggregator):
    base_ts = 1716768000000
    for i in range(5):
        candle = _make_1m_candle(
            "BTCUSDT",
            base_ts + i * 60000,
            67000 + i,
            67500 + i,
            66800 - i,
            67100 + i * 10,
            100 + i * 10,
        )
        results = aggregator.on_1m_close(candle)

    rolled = results[-1]
    assert rolled.tf == "5m"
    assert rolled.open == 67000
    assert rolled.high == 67504
    assert rolled.low == 66796
    assert rolled.close == 67140
    assert rolled.volume == 600


def test_aggregator_15m_rollup(aggregator):
    base_ts = 1716768000000
    for i in range(15):
        results = aggregator.on_1m_close(
            _make_1m_candle(
                "BTCUSDT",
                base_ts + i * 60000,
                67000,
                67000 + i * 10,
                67000 - i * 5,
                67000 + i * 3,
                100,
            )
        )

    assert "15m" in [result.tf for result in results]


def test_aggregator_1h_rollup(aggregator):
    base_ts = 1716768000000
    for i in range(60):
        results = aggregator.on_1m_close(
            _make_1m_candle(
                "BTCUSDT",
                base_ts + i * 60000,
                67000,
                67000 + i,
                67000 - i,
                67000 + i,
                100,
            )
        )

    assert "1h" in [result.tf for result in results]


def test_aggregator_no_rollup_mid_candle(aggregator):
    base_ts = 1716768000000
    for i in range(3):
        results = aggregator.on_1m_close(
            _make_1m_candle("BTCUSDT", base_ts + i * 60000, 67000, 67500, 66800, 67200, 100)
        )

    assert "5m" not in [result.tf for result in results]
    assert "15m" not in [result.tf for result in results]


def test_aggregator_correction_overwrites(aggregator):
    base_ts = 1716768000000
    aggregator.on_1m_close(_make_1m_candle("BTCUSDT", base_ts, 67000, 67500, 66800, 67200, 100))

    correction = KlineCandle(
        symbol="BTCUSDT",
        tf="1m",
        open=67000,
        high=67600,
        low=66800,
        close=67300,
        volume=110,
        open_time=base_ts,
        close_time=base_ts + 59999,
        confirmed=True,
        correction=True,
    )
    aggregator.apply_correction(correction)
    candles = aggregator.get_candles("BTCUSDT", "1m")
    assert len(candles) == 1
    assert candles[0].high == 67600
    assert candles[0].volume == 110
