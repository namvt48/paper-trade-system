import pytest

from app.execution_model import adverse_price
from app.slippage_client import FillService


class StubClient:
    def __init__(self, prices):
        self.prices = iter(prices)
        self.calls = 0

    async def query(self, *args, **kwargs):
        self.calls += 1
        price = next(self.prices)
        return {
            "fallback_used": False, "filled_qty": 1.0, "requested_qty": 1.0,
            "avg_exec_price": price, "book_state": "READY", "source": "live_book",
        }


def test_adverse_price_never_improves_fill():
    assert adverse_price("BUY", 100, 99) == 100
    assert adverse_price("BUY", 100, 101) == 101
    assert adverse_price("SELL", 100, 101) == 100
    assert adverse_price("SELL", 100, 99) == 99


@pytest.mark.asyncio
async def test_latency_model_selects_adverse_delayed_quote():
    slept = []

    async def sleeper(seconds):
        slept.append(seconds)

    client = StubClient([100, 102])
    service = FillService(
        client, 0.05, latency_model_enabled=True, latency_ms=50, sleeper=sleeper,
    )
    resolution = await service.resolve("binance", "BTCUSDT", "LONG", 1, 100, False)
    assert resolution.final_price == 102
    assert resolution.initial_price == 100
    assert resolution.delayed_price == 102
    assert resolution.adverse_movement_bps == pytest.approx(200)
    assert slept == [0.05]
