"""Ensemble support for the shared runner path (CrossSectionalRunnerStrategy).

Mirrors what cross_alpha/engine.py's CrossSectionalEngine already does for the
standalone-container path, but for the runner used by the live alpha-runner
container (registered via runner-config.yaml).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cross_alpha.spec import AlphaSpec
from runner.config import load_runner_config
from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState
from runner.strategies.cross_sectional.strategy import CrossSectionalRunnerStrategy
from runner.strategy.context import StrategyContext


def _bare(params, alphas_root, spec=None):
    s = CrossSectionalRunnerStrategy.__new__(CrossSectionalRunnerStrategy)
    s.alpha_id = "test-alpha"
    s.params = params
    s._alphas_root = alphas_root
    if spec is not None:
        s.spec = spec
    return s


def _write_spec(path, **overrides):
    base = {
        "alpha_id": "member", "timeframe": "1d", "signal": "zscore",
        "params": {"field": "close", "window": 5},
        "universe_size": 4, "universe_mode": "dynamic_top_k", "rebalance_bars": 1,
        "vol_lookback": 10, "ppy": 365, "long_threshold": None, "short_threshold": None,
    }
    base.update(overrides)
    path.write_text(json.dumps(base))


def _ensemble_spec(**overrides) -> AlphaSpec:
    kwargs = dict(
        alpha_id="ensemble-1d", timeframe="1d", signal="ensemble_mean", params={},
        universe_size=180, universe_mode="dynamic_top_k", rebalance_bars=1,
        vol_lookback=30, ppy=365, long_threshold=None, short_threshold=None,
        members=["member-a"], ema_smooth=5,
    )
    kwargs.update(overrides)
    return AlphaSpec(**kwargs)


def _plain_spec(**overrides) -> AlphaSpec:
    kwargs = dict(
        alpha_id="test-kertrend", timeframe="1d", signal="kaufman_trend",
        params={"field": "close", "er_window": 20, "ema_span": 20},
        universe_size=180, universe_mode="dynamic_top_k", rebalance_bars=1,
        vol_lookback=30, ppy=365, long_threshold=None, short_threshold=None,
    )
    kwargs.update(overrides)
    return AlphaSpec(**kwargs)


def test_get_warmup_bars_uses_explicit_value_without_touching_required_bars():
    # ensemble_mean's required_bars always raises (it needs member specs to
    # compute) -- get_warmup_bars must not evaluate it when params already
    # gives an explicit warmup_bars (dict.get's default arg is otherwise
    # evaluated eagerly regardless of whether the key exists).
    strategy = _bare({"warmup_bars": 64}, None, spec=_ensemble_spec())

    assert strategy.get_warmup_bars("1d") == 64


def test_get_warmup_bars_falls_back_to_required_bars_when_not_explicit():
    strategy = _bare({}, None, spec=_plain_spec())

    assert strategy.get_warmup_bars("1d") == 39  # er_window(20)+ema_span(20)-1


def test_get_warmup_bars_raises_for_ensemble_without_explicit_value():
    # Confirms the failure mode is still loud (not silently wrong) when a
    # config entry forgets to set warmup_bars for an ensemble alpha.
    strategy = _bare({}, None, spec=_ensemble_spec())

    with pytest.raises(ValueError, match="members"):
        strategy.get_warmup_bars("1d")


def test_resolve_member_specs_loads_real_alphaspecs(tmp_path):
    (tmp_path / "member-a").mkdir()
    _write_spec(tmp_path / "member-a" / "spec.json", alpha_id="member-a", signal="momentum",
                params={"field": "close", "window": 5})
    (tmp_path / "member-b").mkdir()
    _write_spec(tmp_path / "member-b" / "spec.json", alpha_id="member-b")

    strategy = _bare({}, tmp_path, spec=_ensemble_spec(members=["member-a", "member-b"]))

    resolved = strategy._resolve_member_specs()

    assert [s.alpha_id for s in resolved] == ["member-a", "member-b"]
    assert resolved[0].signal == "momentum"


def test_resolve_member_specs_none_for_non_ensemble_alpha(tmp_path):
    strategy = _bare({}, tmp_path, spec=_plain_spec())

    assert strategy._resolve_member_specs() is None


def test_current_drawdown_tracks_equity_peak():
    strategy = _bare({}, None, spec=_plain_spec())
    strategy._equity = 1.0
    strategy._peak_equity = 1.0
    strategy._last_prices = {"AUSDT": 100.0}
    strategy._base_weights = {"AUSDT": 1.0}
    strategy._portfolio_returns = []
    strategy._pending_cost = 0.0

    strategy._record_portfolio_return({"AUSDT": 90.0})  # -10%

    assert strategy._current_drawdown() == pytest.approx(-0.10)

    strategy._last_prices = {"AUSDT": 90.0}
    strategy._record_portfolio_return({"AUSDT": 120.0})  # new high

    assert strategy._current_drawdown() == pytest.approx(0.0)


NEW_ALPHA_IDS = ["1d-kertrend", "1d-trend60cmf", "1d-vwaprev", "1d-chmom", "1d-iamp", "ensemble-1d"]


@pytest.mark.parametrize("alpha_id", NEW_ALPHA_IDS)
def test_new_alpha_fully_constructs_via_real_runner_config(alpha_id):
    # End-to-end smoke test: load the REAL runner-config.yaml entry (added
    # 2026-07-14) and fully construct CrossSectionalRunnerStrategy against
    # it -- catches anything a narrower unit test could miss (missing
    # whitelist.txt, bad params, ensemble member resolution failing for
    # real, etc.), without needing live Redis/MDS.
    alphas_root = Path(__file__).resolve().parents[2]
    runner_config_path = alphas_root.parent / "runner-config.yaml"
    cfg = load_runner_config(str(runner_config_path))
    entry = next(a for a in cfg.alphas if a.alpha_id == alpha_id)

    ctx = StrategyContext(
        alpha_id=alpha_id,
        version=entry.version,
        cache=SharedCandleCache(),
        signal_dispatcher=None,
        state=StrategyRuntimeState(),
        redis_client=None,
    )
    strategy = CrossSectionalRunnerStrategy(alpha_id, entry.version, dict(entry.params), ctx)

    assert strategy.spec.alpha_id == alpha_id
    assert len(strategy._symbols) > 0
    assert strategy.get_warmup_bars(strategy.spec.timeframe) == entry.params["warmup_bars"]
    if strategy.spec.signal == "ensemble_mean":
        assert strategy._member_specs is not None
        assert len(strategy._member_specs) == len(strategy.spec.members)
    else:
        assert strategy._member_specs is None
