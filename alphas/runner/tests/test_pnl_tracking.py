from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from runner.strategies.cross_sectional.strategy import CrossSectionalRunnerStrategy


def _make_ctx(alpha_id: str = "15m-blend-close"):
    ctx = MagicMock()
    ctx.alpha_id = alpha_id
    ctx.version = "1"
    ctx.redis_client = MagicMock()
    ctx.panel_feature_cache = None
    ctx.warmup_min_symbol_coverage = 0.9
    ctx.state = MagicMock()
    ctx.state.ready = True
    ctx.state.can_open_new_trades.return_value = True
    ctx.state.lease_valid = True
    ctx.cache = MagicMock()
    ctx.signal_dispatcher = None
    ctx.save_positions = MagicMock()
    ctx.load_positions.return_value = {}
    ctx.emit_signal = MagicMock(return_value=None)
    return ctx


def _make_strategy(alpha_id: str = "15m-blend-close") -> CrossSectionalRunnerStrategy:
    ctx = _make_ctx(alpha_id)
    params = {
        "spec_file": "cross_alpha/specs/15m-blend-close.toml",
        "universe_file": "cross_alpha/universe/binance_perp_200.json",
        "exchange": "binance",
        "capital": 10000.0,
        "timeframe": "15m",
        "warmup_bars": 8640,
    }
    with patch.object(CrossSectionalRunnerStrategy, "__init__", lambda self, *a, **kw: None):
        strat = CrossSectionalRunnerStrategy.__new__(CrossSectionalRunnerStrategy)
    strat.alpha_id = alpha_id
    strat.version = "1"
    strat.params = params
    strat.ctx = ctx
    strat._open_positions = {
        "BTCUSDT": {
            "position_id": "test:BTCUSDT:LONG:1000",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry": 65000.0,
            "qty": 0.1,
            "weight": 0.05,
            "strategy_leverage": 1.0,
        },
        "ETHUSDT": {
            "position_id": "test:ETHUSDT:SHORT:1000",
            "symbol": "ETHUSDT",
            "side": "SHORT",
            "entry": 3500.0,
            "qty": 1.0,
            "weight": -0.03,
            "strategy_leverage": 1.0,
        },
    }
    strat._last_pnl_publish = {}
    strat._pnl_channel = f"pnl:{alpha_id}"
    return strat


@pytest.mark.asyncio
async def test_on_price_alert_publishes_pnl_for_long():
    strat = _make_strategy()
    await strat.on_price_alert("BTCUSDT", 66000.0, "bid")

    strat.ctx.redis_client.publish.assert_called_once()
    call_args = strat.ctx.redis_client.publish.call_args
    assert call_args[0][0] == "pnl:15m-blend-close"
    payload = json.loads(call_args[0][1])
    assert payload["symbol"] == "BTCUSDT"
    assert payload["side"] == "LONG"
    assert payload["entry_price"] == 65000.0
    assert payload["current_price"] == 66000.0
    assert abs(payload["pnl_pct"] - (66000.0 - 65000.0) / 65000.0) < 1e-6


@pytest.mark.asyncio
async def test_on_price_alert_publishes_pnl_for_short():
    strat = _make_strategy()
    await strat.on_price_alert("ETHUSDT", 3400.0, "ask")

    strat.ctx.redis_client.publish.assert_called_once()
    call_args = strat.ctx.redis_client.publish.call_args
    assert call_args[0][0] == "pnl:15m-blend-close"
    payload = json.loads(call_args[0][1])
    assert payload["symbol"] == "ETHUSDT"
    assert payload["side"] == "SHORT"
    assert payload["entry_price"] == 3500.0
    assert payload["current_price"] == 3400.0
    assert abs(payload["pnl_pct"] - (3500.0 - 3400.0) / 3500.0) < 1e-6


@pytest.mark.asyncio
async def test_on_price_alert_skips_symbol_with_no_position():
    strat = _make_strategy()
    await strat.on_price_alert("SOLUSDT", 150.0, "bid")

    strat.ctx.redis_client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_on_price_alert_throttles_within_500ms():
    strat = _make_strategy()
    await strat.on_price_alert("BTCUSDT", 66000.0, "bid")
    assert strat.ctx.redis_client.publish.call_count == 1

    await strat.on_price_alert("BTCUSDT", 66100.0, "bid")
    assert strat.ctx.redis_client.publish.call_count == 1

    strat._last_pnl_publish["BTCUSDT"] = time.time() - 1.0
    await strat.on_price_alert("BTCUSDT", 66200.0, "bid")
    assert strat.ctx.redis_client.publish.call_count == 2
