"""Regression: a non-finite selection weight must produce no position/signal.

ZORAUSDT incident: weight=NaN -> notional=NaN -> qty=NaN, and the alpha
published an OPEN with "qty": "nan". The worker stored it as NULL and the
equity collector died with a TypeError for 57 hours.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cross_alpha.engine import CrossSectionalEngine
from cross_alpha.strategy import Selection


def _make_engine(capital: float = 10000.0) -> CrossSectionalEngine:
    with patch.object(CrossSectionalEngine, "__init__", lambda self, *a, **kw: None):
        eng = CrossSectionalEngine.__new__(CrossSectionalEngine)
    eng.alpha_id = "test-cross"
    eng.book_only = False
    eng._open_positions = {}
    eng._base_weights = {}
    eng._portfolio_returns = []
    eng._pending_cost = 0.0
    eng._strategy_leverage = 0.0
    eng._whitelist = set()
    eng.spec = SimpleNamespace(fee_bps=5.0, vol_lookback=10)
    eng.settings = SimpleNamespace(CAPITAL=capital, EXCHANGE="binance")
    eng._is_blacklisted = lambda symbol: False
    eng.can_open_new_trades = lambda: True
    eng._vol_target_leverage = lambda: 5.0
    eng.push_signal = MagicMock()
    eng.mark_positions_changed = MagicMock()
    return eng


def _selection(weights: dict[str, float]) -> Selection:
    return Selection(
        longs=[s for s, w in weights.items() if w > 0],
        shorts=[s for s, w in weights.items() if w < 0],
        scores=dict(weights),
        ranks={},
        weights=weights,
        indicators={},
        diagnostics={},
    )


def test_apply_selection_skips_nan_weight():
    eng = _make_engine()
    prices = {"BTCUSDT": 100.0, "ZORAUSDT": 0.05}

    eng._apply_selection(
        _selection({"BTCUSDT": 0.5, "ZORAUSDT": float("nan")}), prices, 1_000
    )

    assert "ZORAUSDT" not in eng._open_positions
    assert "BTCUSDT" in eng._open_positions
    assert math.isfinite(eng._open_positions["BTCUSDT"]["qty"])
    assert [c.kwargs["symbol"] for c in eng.push_signal.call_args_list] == ["BTCUSDT"]
    for call in eng.push_signal.call_args_list:
        assert math.isfinite(call.kwargs["qty"])
        assert math.isfinite(call.kwargs["entry"])


def test_apply_selection_skips_inf_weight():
    eng = _make_engine()
    prices = {"BTCUSDT": 100.0}

    eng._apply_selection(_selection({"BTCUSDT": float("inf")}), prices, 1_000)

    assert eng._open_positions == {}
    eng.push_signal.assert_not_called()


def test_apply_selection_valid_weights_still_open():
    eng = _make_engine()
    prices = {"BTCUSDT": 100.0}

    eng._apply_selection(_selection({"BTCUSDT": 0.5}), prices, 1_000)

    pos = eng._open_positions["BTCUSDT"]
    assert pos["qty"] == 10000.0 * 0.5 * 5.0 / 100.0
    eng.push_signal.assert_called_once()
    assert eng.push_signal.call_args.kwargs["qty"] == pos["qty"]
