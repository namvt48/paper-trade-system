from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from cross_alpha.engine import CrossSectionalEngine
from cross_alpha.spec import AlphaSpec


def _make_engine() -> CrossSectionalEngine:
    with patch.object(CrossSectionalEngine, "__init__", lambda self, *a, **kw: None):
        eng = CrossSectionalEngine.__new__(CrossSectionalEngine)
    eng.alpha_id = "15m-blend-close"
    eng._open_positions = {
        "BTCUSDT": {
            "position_id": "test:BTCUSDT:LONG:1000",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry": 65000.0,
            "qty": 0.1,
            "weight": 0.05,
        },
    }
    eng._last_pnl_publish = {}
    eng._pnl_channel = "pnl:15m-blend-close"
    return eng


def _make_pnl_engine() -> CrossSectionalEngine:
    with patch.object(CrossSectionalEngine, "__init__", lambda self, *a, **kw: None):
        eng = CrossSectionalEngine.__new__(CrossSectionalEngine)
    eng.spec = AlphaSpec(
        alpha_id="test-ensemble", timeframe="1d", signal="ensemble_mean", params={},
        universe_size=4, universe_mode="dynamic_top_k", rebalance_bars=1,
        vol_lookback=10, ppy=365, long_threshold=None, short_threshold=None,
    )
    eng._portfolio_returns = []
    eng._last_prices = {}
    eng._base_weights = {"AUSDT": 1.0}
    eng._pending_cost = 0.0
    eng._peak_equity = 1.0
    eng._equity = 1.0
    return eng


@pytest.mark.asyncio
async def test_on_price_alert_message_publishes_pnl():
    eng = _make_engine()
    msg = {"symbol": "BTCUSDT", "bid": 66000.0, "ask": 66010.0, "side": "bid"}

    with patch("base.signal_push._r") as mock_redis:
        eng.on_price_alert_message(msg)
        mock_redis.publish.assert_called_once()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "pnl:15m-blend-close"
        payload = json.loads(call_args[0][1])
        assert payload["symbol"] == "BTCUSDT"
        assert payload["side"] == "LONG"
        assert payload["current_price"] == 66000.0
        assert abs(payload["pnl_pct"] - (66000.0 - 65000.0) / 65000.0) < 1e-6


@pytest.mark.asyncio
async def test_on_price_alert_message_skips_no_position():
    eng = _make_engine()
    msg = {"symbol": "SOLUSDT", "bid": 150.0, "ask": 150.1, "side": "bid"}

    with patch("base.signal_push._r") as mock_redis:
        eng.on_price_alert_message(msg)
        mock_redis.publish.assert_not_called()


@pytest.mark.asyncio
async def test_on_price_alert_message_throttles():
    eng = _make_engine()
    msg = {"symbol": "BTCUSDT", "bid": 66000.0, "ask": 66010.0, "side": "bid"}

    with patch("base.signal_push._r") as mock_redis:
        eng.on_price_alert_message(msg)
        assert mock_redis.publish.call_count == 1

        eng.on_price_alert_message(msg)
        assert mock_redis.publish.call_count == 1  # throttled

        eng._last_pnl_publish["BTCUSDT"] = time.time() - 1.0
        eng.on_price_alert_message(msg)
        assert mock_redis.publish.call_count == 2


def test_drawdown_is_zero_at_a_new_equity_high():
    eng = _make_pnl_engine()
    eng._last_prices = {"AUSDT": 100.0}

    eng._record_portfolio_return({"AUSDT": 110.0})  # +10% -> new high

    assert eng._equity == pytest.approx(1.10)
    assert eng._peak_equity == pytest.approx(1.10)
    assert eng._current_drawdown() == pytest.approx(0.0)


def test_drawdown_tracks_decline_from_peak():
    eng = _make_pnl_engine()
    eng._last_prices = {"AUSDT": 100.0}

    eng._record_portfolio_return({"AUSDT": 110.0})  # equity 1.10, peak 1.10
    eng._last_prices = {"AUSDT": 110.0}
    eng._record_portfolio_return({"AUSDT": 99.0})   # -10% -> equity 0.99

    assert eng._equity == pytest.approx(1.10 * 0.9)
    assert eng._peak_equity == pytest.approx(1.10)
    assert eng._current_drawdown() == pytest.approx((1.10 * 0.9 - 1.10) / 1.10)


class _FakeConfig:
    def __init__(self, alpha_id: str, spec_file: str):
        self.ALPHA_ID = alpha_id
        self.SPEC_FILE = spec_file
        self.SYMBOL_BLACKLIST = ""
        self.SYMBOL_WHITELIST = ""


def _write_spec(path, **overrides):
    base = {
        "alpha_id": "member", "timeframe": "1d", "signal": "zscore",
        "params": {"field": "close", "window": 5},
        "universe_size": 4, "universe_mode": "dynamic_top_k", "rebalance_bars": 1,
        "vol_lookback": 10, "ppy": 365, "long_threshold": None, "short_threshold": None,
    }
    base.update(overrides)
    path.write_text(json.dumps(base))


def test_init_resolves_member_specs_for_ensemble_alpha(tmp_path):
    (tmp_path / "1d-member-a").mkdir()
    _write_spec(tmp_path / "1d-member-a" / "spec.json", alpha_id="1d-member-a")
    (tmp_path / "1d-member-b").mkdir()
    _write_spec(tmp_path / "1d-member-b" / "spec.json", alpha_id="1d-member-b", signal="momentum")

    ensemble_dir = tmp_path / "ensemble-1d"
    ensemble_dir.mkdir()
    _write_spec(
        ensemble_dir / "spec.json",
        alpha_id="ensemble-1d", signal="ensemble_mean", params={},
        members=["1d-member-a", "1d-member-b"], overlay=None, ema_smooth=5,
    )

    with patch("base.engine.BaseEngine._load_whitelist_file", lambda self: None), \
         patch("base.engine.BaseEngine._load_blacklist_file", lambda self: None):
        eng = CrossSectionalEngine(_FakeConfig("ensemble-1d", str(ensemble_dir / "spec.json")))

    assert eng._member_specs is not None
    assert [s.alpha_id for s in eng._member_specs] == ["1d-member-a", "1d-member-b"]
    assert eng._member_specs[1].signal == "momentum"


def test_init_leaves_member_specs_none_for_non_ensemble_alpha(tmp_path):
    spec_dir = tmp_path / "1d-kertrend"
    spec_dir.mkdir()
    _write_spec(spec_dir / "spec.json", alpha_id="1d-kertrend", signal="kaufman_trend",
                params={"field": "close", "er_window": 20, "ema_span": 20})

    with patch("base.engine.BaseEngine._load_whitelist_file", lambda self: None), \
         patch("base.engine.BaseEngine._load_blacklist_file", lambda self: None):
        eng = CrossSectionalEngine(_FakeConfig("1d-kertrend", str(spec_dir / "spec.json")))

    assert eng._member_specs is None


def test_ensemble_1d_resolves_its_4_real_member_specs():
    # Integration check tying U4-U14 together: the actual alphas/ensemble-1d
    # against the actual alphas/1d-{trend60cmf,kertrend,vwaprev,chmom,iamp}
    # built earlier in this plan -- not a synthetic tmp_path fixture.
    from pathlib import Path

    alphas_root = Path(__file__).resolve().parents[2]
    spec_file = alphas_root / "ensemble-1d" / "spec.json"

    with patch("base.engine.BaseEngine._load_whitelist_file", lambda self: None), \
         patch("base.engine.BaseEngine._load_blacklist_file", lambda self: None):
        eng = CrossSectionalEngine(_FakeConfig("ensemble-1d", str(spec_file)))

    assert eng._member_specs is not None
    assert {s.alpha_id for s in eng._member_specs} == {
        "1d-trend60cmf", "1d-kertrend", "1d-vwaprev", "1d-chmom", "1d-iamp",
    }
    by_id = {s.alpha_id: s for s in eng._member_specs}
    assert by_id["1d-chmom"].needs_funding is True
    assert by_id["1d-trend60cmf"].signal == "trend_cmf_blend"
    assert by_id["1d-kertrend"].signal == "kaufman_trend"
    assert by_id["1d-vwaprev"].signal == "vwap_reversion"
    assert by_id["1d-iamp"].signal == "ideal_amplitude"


def test_drawdown_recovers_after_a_new_high():
    eng = _make_pnl_engine()
    eng._last_prices = {"AUSDT": 100.0}
    eng._record_portfolio_return({"AUSDT": 90.0})  # -10% drawdown
    assert eng._current_drawdown() < 0

    eng._last_prices = {"AUSDT": 90.0}
    eng._record_portfolio_return({"AUSDT": 120.0})  # new high

    assert eng._current_drawdown() == pytest.approx(0.0)
    assert eng._peak_equity == pytest.approx(eng._equity)
