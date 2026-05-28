from unittest.mock import AsyncMock

import pytest

from app.aggregator import Aggregator
from app.models import KlineCandle
from app.reconciler import Reconciler


@pytest.fixture
def aggregator():
    return Aggregator(timeframes=["1m", "15m", "1h"])


def test_is_candle_boundary(aggregator):
    reconciler = Reconciler(aggregator=aggregator, reconcile_tfs=["15m", "1h"])
    assert reconciler.is_candle_boundary(1716768000000, "15m") is True
    assert reconciler.is_candle_boundary(1716768000000, "1h") is True
    assert reconciler.is_candle_boundary(1716768060000, "15m") is False


def test_should_reconcile(aggregator):
    reconciler = Reconciler(aggregator=aggregator, reconcile_tfs=["15m", "1h"])
    assert reconciler.should_reconcile(1716768000000) is True
    assert reconciler.should_reconcile(1716768060000) is False


@pytest.mark.asyncio
async def test_reconcile_symbol_detects_mismatch(aggregator):
    reconciler = Reconciler(aggregator=aggregator, reconcile_tfs=["15m"], reconcile_delay=0)
    base_ts = 1716768000000
    for i in range(15):
        aggregator.on_1m_close(
            KlineCandle(
                symbol="BTCUSDT",
                tf="1m",
                open=67000,
                high=67500,
                low=66800,
                close=67200,
                volume=100,
                open_time=base_ts + i * 60000,
                close_time=base_ts + i * 60000 + 59999,
            )
        )

    client = AsyncMock()
    client.futures_klines = AsyncMock(
        return_value=[
            [base_ts, "67000", "67600", "66800", "67300", "110", base_ts + 15 * 60000 - 1],
            [base_ts + 15 * 60000, "67300", "67800", "67000", "67500", "120", base_ts + 30 * 60000 - 1],
        ]
    )

    corrections = await reconciler.reconcile_symbol(client, "BTCUSDT", "15m")
    assert len(corrections) == 1
    assert corrections[0].high == 67600
    assert corrections[0].volume == 110
    assert corrections[0].correction is True
