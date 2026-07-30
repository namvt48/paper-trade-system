from __future__ import annotations

import pytest

from portfolio_manager.core.blend import blend_books, build_blend_outputs
from portfolio_manager.core.book import TargetBook, TargetBookStore
from portfolio_manager.core.engine import PortfolioEngine
from portfolio_manager.core.overlays import ema_smooth, gross_target, per_coin_cap
from portfolio_manager.core.regime import btc_trend_state


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def publish(self, channel: str, value: str) -> None:
        self.published.append((channel, value))


def _book(sleeve_id: str, weights: dict[str, float]) -> TargetBook:
    return TargetBook.create(
        sleeve_id,
        "1h",
        weights,
        revision=1,
        as_of_candle_ms=1,
        generated_at="2099-01-01T00:00:00+00:00",
    )


def test_target_book_round_trip_and_notification() -> None:
    redis = FakeRedis()
    store = TargetBookStore(redis)
    book = _book("sleeve-a", {"BTCUSDT": 0.5, "ETHUSDT": -0.5})
    store.write(book)

    loaded = store.read("sleeve-a")
    assert loaded is not None
    assert loaded.to_dict() == book.to_dict()
    assert redis.published == [("book:updated:sleeve-a", "1")]


def test_fixed_blend_drops_stale_sleeve_without_reusing_weights() -> None:
    books = {"a": _book("a", {"BTCUSDT": 1.0}), "b": None}
    weights = {"a": 0.5, "b": 0.5}
    blended, active, stale = blend_books(books, weights, {"a": 60, "b": 60})
    assert blended == {"BTCUSDT": 0.5}
    assert active == ("a",)
    assert stale == ("b",)


def test_blend_candidate_is_throttled_but_baseline_is_unchanged() -> None:
    result = build_blend_outputs(
        {"a": _book("a", {"BTCUSDT": 1.0})},
        {"a": 1.0},
        {"a": 60},
        cap=1.0,
        gross=1.0,
        regime_state={"ready": True, "downtrend": True, "return": -0.2},
        downtrend_multiplier=0.25,
    )
    assert result.baseline["BTCUSDT"] == pytest.approx(1.0)
    assert result.candidate["BTCUSDT"] == pytest.approx(0.25)


def test_stale_sleeve_is_not_reintroduced_by_ema() -> None:
    result = build_blend_outputs(
        {"a": None},
        {"a": 1.0},
        {"a": 60},
        cap=1.0,
        gross=1.0,
        previous={"BTCUSDT": 1.0},
        ema_span=5,
    )
    assert result.baseline == {}


def test_overlay_math() -> None:
    assert per_coin_cap({"A": 0.8, "B": -0.2}, 0.5) == {"A": 0.5, "B": -0.2}
    assert gross_target({"A": 0.2, "B": -0.2}, 1.0) == {"A": 0.5, "B": -0.5}
    assert ema_smooth({"A": 1.0}, {"A": 0.0}, 1) == {"A": 1.0}


def test_btc_trend_uses_completed_close_window() -> None:
    state = btc_trend_state([100.0, 99.0, 98.0, 97.0], lookback=2)
    assert state["ready"] is True
    assert state["downtrend"] is True
    assert state["return"] == pytest.approx(97.0 / 99.0 - 1.0)


def test_engine_rejects_unimplemented_on_stale_policy() -> None:
    config = {
        "sleeves": [
            {
                "id": "a",
                "weight": 1.0,
                "max_staleness_sec": 60,
                "on_stale": "hold_last",
            },
        ],
    }
    with pytest.raises(ValueError, match="hold_last"):
        PortfolioEngine(config, FakeRedis())
