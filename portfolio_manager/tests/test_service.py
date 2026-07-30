from __future__ import annotations

import json

import pytest

from portfolio_manager.app.service import PortfolioService
from portfolio_manager.core.book import TargetBook, TargetBookStore


class FakeRedis:
    def __init__(self, snapshot_positions: list[dict] | None = None) -> None:
        self._snapshot_positions = snapshot_positions or []
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        if key.startswith("paper:positions:snapshot:"):
            return json.dumps({"positions": self._snapshot_positions})
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def publish(self, channel: str, value: str) -> None:
        pass


def _position(symbol: str, side: str, weight: float, position_id: str = "p1") -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "position_id": position_id,
        "metadata": json.dumps({"weight": weight}),
    }


def _service(
    snapshot_positions: list[dict] | None = None, config_overrides: dict | None = None
):
    config = {
        "alpha_id": "pm-live",
        "capital": 100_000.0,
        "exchange": "binance",
        "fee_bps": 7.0,
        "sleeves": [],
        "execution": {"enabled": True},
        "regime": {"execution_enabled": False},
        "overlay": {},
    }
    config.update(config_overrides or {})
    captured: list[tuple[str, dict]] = []
    redis = FakeRedis(snapshot_positions)
    service = PortfolioService(
        config, redis, publish=lambda t, **f: captured.append((t, f))
    )
    return service, captured


def test_same_side_resize_closes_before_reopening():
    # A resize (0.03 -> 0.05, same LONG side) must CLOSE the old position
    # before OPENing the new one -- the worker rejects a second OPEN on the
    # same (alpha_id, symbol) while one is still open, so OPEN-only would
    # leave the position stuck at the old weight forever.
    service, captured = _service(
        snapshot_positions=[_position("BTCUSDT", "LONG", 0.03)]
    )

    published = service._reconcile_and_publish({"BTCUSDT": 0.05}, {"BTCUSDT": 100.0})

    assert published == 2
    assert captured[0][0] == "CLOSE"
    assert captured[0][1]["position_id"] == "p1"
    assert captured[1][0] == "OPEN"
    assert captured[1][1]["symbol"] == "BTCUSDT"
    assert captured[1][1]["side"] == "LONG"
    assert captured[1][1]["qty"] == pytest.approx(100_000.0 * 0.05 / 100.0)


def test_unchanged_weight_is_not_republished():
    service, captured = _service(
        snapshot_positions=[_position("BTCUSDT", "LONG", 0.05)]
    )

    published = service._reconcile_and_publish({"BTCUSDT": 0.05}, {"BTCUSDT": 100.0})

    assert published == 0
    assert captured == []


def test_side_flip_closes_then_reopens_opposite_side():
    service, captured = _service(
        snapshot_positions=[_position("BTCUSDT", "LONG", 0.05)]
    )

    published = service._reconcile_and_publish({"BTCUSDT": -0.02}, {"BTCUSDT": 100.0})

    assert published == 2
    assert captured[0][0] == "CLOSE"
    assert captured[1][0] == "OPEN"
    assert captured[1][1]["side"] == "SHORT"


def test_new_symbol_opens_without_a_close():
    service, captured = _service(snapshot_positions=[])

    published = service._reconcile_and_publish({"ETHUSDT": 0.04}, {"ETHUSDT": 50.0})

    assert published == 1
    assert captured[0][0] == "OPEN"


def test_target_to_zero_closes_without_reopening():
    service, captured = _service(
        snapshot_positions=[_position("BTCUSDT", "LONG", 0.05)]
    )

    published = service._reconcile_and_publish({"BTCUSDT": 0.0}, {"BTCUSDT": 100.0})

    assert published == 1
    assert captured[0][0] == "CLOSE"


def test_execution_disabled_never_publishes_even_with_regime_on():
    # A non-empty candidate must exist (a real book, not a missing/stale one)
    # so this proves the execution flag itself blocks publishing -- not that
    # there was simply nothing to publish.
    redis = FakeRedis([])
    store = TargetBookStore(redis)
    store.write(
        TargetBook.create(
            "sleeve-a",
            "1h",
            {"BTCUSDT": 1.0},
            revision=1,
            as_of_candle_ms=1,
            meta={"prices": {"BTCUSDT": 100.0}},
        )
    )
    config = {
        "alpha_id": "pm-live",
        "capital": 100_000.0,
        "exchange": "binance",
        "fee_bps": 7.0,
        "sleeves": [{"id": "sleeve-a", "weight": 1.0, "max_staleness_sec": 1e9}],
        "execution": {"enabled": False},
        "regime": {"execution_enabled": True},
        "overlay": {"per_coin_cap": 1.0, "gross_target": 1.0},
    }
    captured: list[tuple[str, dict]] = []
    service = PortfolioService(
        config, redis, publish=lambda t, **f: captured.append((t, f))
    )

    result = service.run_cycle(regime_state={"downtrend": False})

    assert result["candidate"] != {}
    assert result["published"] == 0
    assert result["execution_enabled"] is False
    assert captured == []


def test_regime_disabled_still_executes_baseline_when_pm_execution_enabled():
    # R13: regime.execution_enabled=false must NOT also silence PM execution
    # -- PM must still publish the untouched baseline book, only skipping
    # the throttle. Conflating the two flags was the original bug.
    redis = FakeRedis([])
    store = TargetBookStore(redis)
    store.write(
        TargetBook.create(
            "sleeve-a",
            "1h",
            {"BTCUSDT": 1.0},
            revision=1,
            as_of_candle_ms=1,
            meta={"prices": {"BTCUSDT": 100.0}},
        )
    )
    config = {
        "alpha_id": "pm-live",
        "capital": 100_000.0,
        "exchange": "binance",
        "fee_bps": 7.0,
        "sleeves": [{"id": "sleeve-a", "weight": 1.0, "max_staleness_sec": 1e9}],
        "execution": {"enabled": True},
        "regime": {"execution_enabled": False},
        "overlay": {"per_coin_cap": 1.0, "gross_target": 1.0},
    }
    captured: list[tuple[str, dict]] = []
    service = PortfolioService(
        config, redis, publish=lambda t, **f: captured.append((t, f))
    )

    result = service.run_cycle(regime_state={"downtrend": True})

    assert result["regime_execution_enabled"] is False
    assert result["selected"] == result["baseline"]
    assert result["published"] == 1
    assert captured[0][0] == "OPEN"
