"""Regression: book-only shadow sleeves must survive a runner restart.

2026-07-27 incident: the alpha-runner process restarts multiple times a day
(warmup cycles ~50min-1h apart, observed several times within a single day),
while book-only sleeve alphas (`*-sleeve`) only rebalance every 24-96
candles. `_shadow_equity`/`_base_weights`/`_last_prices` lived only in the
strategy instance's memory with no restore path (unlike real positions,
which reconcile from the worker's authoritative DB via
`reconcile_open_positions`) -- every restart silently reset the sleeve's
published equity back to 1.0 and blanked `_base_weights` until the next
rebalance repopulated it. Rendered as an equity curve this looked like a
repeated snap-back to baseline ("square"/blocky) instead of a continuously
compounding curve like every other (real-position) alpha.
"""

from __future__ import annotations

import json

import pytest

from cross_alpha.strategy import Selection
from portfolio_manager.core.book import TargetBookStore
from runner.strategies.cross_sectional.strategy import CrossSectionalRunnerStrategy


class _FakeSpec:
    timeframe = "1h"
    fee_bps = 7.0
    vol_lookback = 30
    max_leverage = 3.0
    target_vol = 0.1
    ppy = 365


class _FakeRedis:
    """Dict-backed sync redis double -- enough for TargetBookStore + strategy.get/set."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def publish(self, channel, value):
        return None


class _FakeCtx:
    def __init__(self, redis_client):
        self.redis_client = redis_client


def _selection(weights: dict[str, float]) -> Selection:
    longs = sorted(s for s, w in weights.items() if w > 0)
    shorts = sorted(s for s, w in weights.items() if w < 0)
    return Selection(
        longs=longs,
        shorts=shorts,
        scores={},
        ranks={},
        weights=weights,
        indicators={},
        diagnostics={},
    )


def _make_strategy(redis_client, alpha_id="test-sleeve"):
    strat = CrossSectionalRunnerStrategy.__new__(CrossSectionalRunnerStrategy)
    strat.alpha_id = alpha_id
    strat.ctx = _FakeCtx(redis_client)
    strat.spec = _FakeSpec()
    strat.capital = 10_000.0
    strat.exchange = "binance"
    strat.book_only = True
    strat._book_store = TargetBookStore(redis_client)
    strat._book_revision = 0
    strat._portfolio_returns = []
    strat._pending_cost = 0.0
    strat._equity = 1.0
    strat._peak_equity = 1.0
    strat._shadow_equity = 1.0
    strat._last_prices = {}
    strat._base_weights = {}
    return strat


def test_restore_is_a_no_op_for_a_brand_new_alpha_with_no_prior_state():
    strategy = _make_strategy(_FakeRedis())

    strategy._restore_shadow_state()

    assert strategy._shadow_equity == 1.0
    assert strategy._equity == 1.0
    assert strategy._peak_equity == 1.0
    assert strategy._base_weights == {}
    assert strategy._last_prices == {}


def test_restore_recovers_equity_peak_and_last_prices_from_shadow_pnl():
    redis_client = _FakeRedis()
    redis_client.set(
        "shadow:pnl:test-sleeve",
        json.dumps(
            {
                "equity": 1.075,
                "peak_equity": 1.12,
                "prices": {"BTCUSDT": 110.0, "ETHUSDT": 190.0},
            }
        ),
    )
    strategy = _make_strategy(redis_client)

    strategy._restore_shadow_state()

    assert strategy._shadow_equity == pytest.approx(1.075)
    assert strategy._equity == pytest.approx(1.075)
    assert strategy._peak_equity == pytest.approx(1.12)
    assert strategy._last_prices == {"BTCUSDT": 110.0, "ETHUSDT": 190.0}


def test_restore_recovers_base_weights_from_target_book():
    redis_client = _FakeRedis()
    strategy_a = _make_strategy(redis_client)
    strategy_a._publish_target_book(
        _selection({"BTCUSDT": 0.5, "ETHUSDT": -0.5}),
        {"BTCUSDT": 100.0, "ETHUSDT": 200.0},
        candle_open_ms=1_000,
    )

    strategy_b = _make_strategy(redis_client)
    strategy_b._restore_shadow_state()

    assert strategy_b._base_weights == {"BTCUSDT": 0.5, "ETHUSDT": -0.5}


def test_restart_no_longer_resets_shadow_equity_to_baseline():
    """The actual field bug: a fresh strategy instance (simulating a runner
    restart) must continue compounding from the prior instance's equity
    instead of snapping back to 1.0."""
    redis_client = _FakeRedis()

    strategy_a = _make_strategy(redis_client)
    strategy_a._base_weights = {"BTCUSDT": 0.5, "ETHUSDT": -0.5}
    strategy_a._last_prices = {"BTCUSDT": 100.0, "ETHUSDT": 200.0}
    strategy_a._publish_target_book(
        _selection(strategy_a._base_weights),
        strategy_a._last_prices,
        candle_open_ms=1_000,
    )
    # One candle of mark-to-market before the "restart".
    strategy_a._record_portfolio_return({"BTCUSDT": 110.0, "ETHUSDT": 190.0})
    equity_before_restart = strategy_a._shadow_equity
    assert equity_before_restart == pytest.approx(1.075)

    # Simulate a runner restart: a brand-new strategy instance, sharing only
    # the same (persistent, separate-container) Redis.
    strategy_b = _make_strategy(redis_client)
    strategy_b._restore_shadow_state()

    assert strategy_b._shadow_equity == pytest.approx(equity_before_restart)
    assert strategy_b._base_weights == {"BTCUSDT": 0.5, "ETHUSDT": -0.5}

    # The next candle's mark must compound on top of the restored equity,
    # not restart from 1.0.
    strategy_b._record_portfolio_return({"BTCUSDT": 121.0, "ETHUSDT": 180.0})
    assert strategy_b._shadow_equity > equity_before_restart
