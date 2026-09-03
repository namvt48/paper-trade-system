"""Correctness coverage for the top10_vote_combo runner strategy.

Groups:
1. Factor math -- hand-computed expected values for all 6 factors + chg/z
   transforms on a synthetic panel (verbatim docs/run_top10.py port).
2. Voting -- cross-section z -> +/-1 votes; majority sign wins; 5-5 tie abstains.
3. Weights -- sum(|w|) == 1 over traded symbols; abstained symbols get none.
4. Signal shape -- CLOSE(REBALANCE) first, then OPENs with correct
   side/qty/leverage/fee/tf/metadata.
5. Wiring -- channels, warmup symbols (54 from the whitelist file), warmup bars,
   coverage gate + held-position bypass (2026-08-21 incident fix).
6. register() -- StrategyRegistry registration of "top10_vote_combo".
7. No-data / insufficient-data paths -- no signals, no crash.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest

from runner.strategies.top10_vote_combo.strategy import (
    DEFAULT_WARMUP_BARS,
    D1_MS,
    MIN_CLOSED_BARS,
    SelectionPayload,
    TOP10_SPECS,
    TRANSFORMS,
    Top10VoteComboRunnerStrategy,
    VoteSelection,
    aggregate_votes,
    build_vote_matrix,
    compute_factors,
    cross_section_votes,
)

ALPHAS_ROOT = Path(__file__).resolve().parents[2]
BASE_MS = 1_786_000_000_000  # realistic ms epoch (> 1e12, not seconds)
SYMBOLS = ("BTCUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")


# --------------------------------------------------------------- group 1: factors


def synth_panel(n: int = 150, symbols=SYMBOLS) -> dict[str, pd.DataFrame]:
    """Synthetic 1d panel with hand-computable factor values.

    Bars 0..n-2 (flat): O=100, H=110, L=90, C=105, V=1000
        -> lower_shadow = (100-90)/20 = 1/2, upper_shadow = 1/2, body = clv = 0,
           ret = 0
    Bar n-1 (last):     O=105, H=115, L=85, C=110, V=1000
        -> H-L = 30, min(O,C) = 105, max(O,C) = 110
           lower_shadow = (105-85)/30   = 2/3
           upper_shadow = (115-110)/30  = 1/6
           body         = |110-105|/30  = 1/6
           clv          = ((110-85)-(115-110))/30 = 2/3
           ret          = 110/105 - 1   = 1/21
    Every symbol shares the SAME series, so beta vs BTCUSDT is exactly 1 and
    residual_returns is exactly 0. Volume is constant 1000, so the last
    quote-volume proxy value is 110 * 1000 = 110_000.
    """
    index = list(range(n))

    def panel_of(flat: float, last: float) -> pd.DataFrame:
        return pd.DataFrame(
            {s: [flat] * (n - 1) + [last] for s in symbols}, index=index
        )

    open_ = panel_of(100.0, 105.0)
    high = panel_of(110.0, 115.0)
    low = panel_of(90.0, 85.0)
    close = panel_of(105.0, 110.0)
    volume = panel_of(1000.0, 1000.0)
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "quote_volume": close * volume,  # documented proxy
    }


def test_factor_math_hand_computed_on_synthetic_panel() -> None:
    P = synth_panel()
    F = compute_factors(P)
    assert set(F) == {
        "ohlc_vol:lower_shadow",
        "ohlc_vol:upper_shadow",
        "ohlc_vol:body",
        "ohlc_vol:clv",
        "liquidity:spread_ar",
        "residual:residual_returns",
    }
    for symbol in SYMBOLS:
        assert F["ohlc_vol:lower_shadow"][symbol].iloc[-1] == pytest.approx(2 / 3)
        assert F["ohlc_vol:upper_shadow"][symbol].iloc[-1] == pytest.approx(1 / 6)
        assert F["ohlc_vol:body"][symbol].iloc[-1] == pytest.approx(1 / 6)
        assert F["ohlc_vol:clv"][symbol].iloc[-1] == pytest.approx(2 / 3)
        # Invariant for every bar with H > L: lower + upper + body == 1
        ident = (
            F["ohlc_vol:lower_shadow"][symbol]
            + F["ohlc_vol:upper_shadow"][symbol]
            + F["ohlc_vol:body"][symbol]
        ).dropna()
        assert float((ident - 1.0).abs().max()) < 1e-12
        # spread_ar last bar: rolling20 mean of |ret|/qv; only the last bar is
        # nonzero -> ((1/21) / 110_000) / 20
        assert F["liquidity:spread_ar"][symbol].iloc[-1] == pytest.approx(
            (1 / 21) / 110_000 / 20
        )
        # Identical series -> beta == 1 -> residual == 0
        assert F["residual:residual_returns"][symbol].iloc[-1] == pytest.approx(
            0.0, abs=1e-12
        )


def test_transforms_hand_computed_on_synthetic_panel() -> None:
    P = synth_panel()
    F = compute_factors(P)
    ls = F["ohlc_vol:lower_shadow"]
    # chg at the last bar: 2/3 - 1/2 (every lookback row is the flat 1/2)
    for name in ("chg5", "chg20", "chg60"):
        assert TRANSFORMS[name](ls).iloc[-1]["BTCUSDT"] == pytest.approx(1 / 6)
    # z60 last bar: window = 59 x 1/2 + 1 outlier at 2/3.
    # Closed form for (n-1) equal values + 1 outlier: z = (n-1)/sqrt(n).
    assert TRANSFORMS["z60"](ls).iloc[-1]["BTCUSDT"] == pytest.approx(
        59 / math.sqrt(60)
    )
    # z120 last bar: window = 119 x 1/2 + 1 outlier -> 119/sqrt(120)
    assert TRANSFORMS["z120"](ls).iloc[-1]["BTCUSDT"] == pytest.approx(
        119 / math.sqrt(120)
    )


# --------------------------------------------------------------- group 2: voting


def test_cross_section_votes_sign_flip_and_zero_at_mean() -> None:
    x = pd.Series({"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 5.0})
    # cs_z = [-1.2649, -0.6325, 0, +0.6325, +1.2649] (mean 3, std(ddof=1) sqrt(2.5))
    votes = cross_section_votes(x, sign=-1)
    assert votes["A"] == 1.0  # lowest factor value + sign -1 -> votes LONG
    assert votes["B"] == 1.0
    assert votes["C"] == 0.0  # exactly at the mean -> abstain
    assert votes["D"] == -1.0
    assert votes["E"] == -1.0
    votes_pos = cross_section_votes(x, sign=1)
    assert votes_pos["A"] == -1.0 and votes_pos["E"] == 1.0


def test_cross_section_votes_zero_std_abstains() -> None:
    x = pd.Series({"A": 2.0, "B": 2.0, "C": 2.0, "D": 2.0})
    assert (cross_section_votes(x, sign=-1) == 0).all()


def test_cross_section_votes_all_nan_abstains() -> None:
    x = pd.Series({"A": np.nan, "B": np.nan, "C": np.nan})
    votes = cross_section_votes(x, sign=1)
    assert (votes == 0).all()
    assert list(votes.index) == ["A", "B", "C"]


def test_cross_section_votes_nan_symbols_abstain_individually() -> None:
    x = pd.Series({"A": 1.0, "B": np.nan, "C": 5.0, "D": 2.0, "E": 4.0})
    votes = cross_section_votes(x, sign=1)
    assert votes["B"] == 0.0  # NaN factor -> this symbol abstains
    assert votes["E"] == 1.0  # highest value with sign +1


def test_build_vote_matrix_uses_all_10_specs_with_btc() -> None:
    P = synth_panel()  # BTCUSDT present (identical series -> all votes abstain)
    matrix = build_vote_matrix(P)
    assert matrix.shape[0] == len(TOP10_SPECS) == 10
    assert set(matrix.index) == {spec["name"] for spec in TOP10_SPECS}
    assert set(matrix.columns) == set(SYMBOLS)
    assert matrix.isin([-1.0, 0.0, 1.0]).all().all()


def test_build_vote_matrix_skips_residual_without_btc() -> None:
    P = synth_panel(symbols=("AAAUSDT", "BBBUSDT", "CCCUSDT"))
    matrix = build_vote_matrix(P)
    assert matrix.shape[0] == 9
    assert "residual_returns|chg60|-" not in matrix.index


_VOTE_ROWS = (
    # LONG_S SHORT_S TIE_S FLAT_S
    (1, -1, 1, 0),
    (1, -1, 1, 0),
    (1, -1, 1, 0),
    (1, -1, 1, 0),
    (1, -1, 1, 0),
    (1, -1, -1, 0),
    (1, -1, -1, 0),
    (-1, 1, -1, 0),
    (-1, 1, -1, 0),
    (-1, -1, -1, 0),
)
_VOTE_COLS = ("LONG_S", "SHORT_S", "TIE_S", "FLAT_S")


def _vote_matrix() -> pd.DataFrame:
    return pd.DataFrame(
        _VOTE_ROWS, columns=_VOTE_COLS, index=[f"alpha{i}" for i in range(10)]
    )


# --------------------------------------------------- groups 2+3: direction/weights


def test_votes_majority_sign_wins_and_tie_abstains() -> None:
    sel = aggregate_votes(_vote_matrix())
    assert sel.vote_sums["LONG_S"] == 4  # 7 up, 3 down -> LONG
    assert sel.vote_sums["SHORT_S"] == -6  # 2 up, 8 down -> SHORT
    assert sel.vote_sums["TIE_S"] == 0  # 5 up, 5 down -> NO TRADE
    assert sel.vote_sums["FLAT_S"] == 0
    assert sel.up_votes["LONG_S"] == 7 and sel.down_votes["LONG_S"] == 3
    assert sel.up_votes["SHORT_S"] == 2 and sel.down_votes["SHORT_S"] == 8
    assert sel.weights["LONG_S"] > 0  # LONG direction
    assert sel.weights["SHORT_S"] < 0  # SHORT direction
    assert "TIE_S" not in sel.weights


def test_weights_normalized_to_gross_one_and_no_weight_for_abstainers() -> None:
    sel = aggregate_votes(_vote_matrix())
    assert sum(abs(w) for w in sel.weights.values()) == pytest.approx(1.0)
    assert sel.weights["LONG_S"] == pytest.approx(4 / 10)
    assert sel.weights["SHORT_S"] == pytest.approx(-6 / 10)
    assert sel.scores["LONG_S"] == pytest.approx(4 / 10)
    assert sel.scores["SHORT_S"] == pytest.approx(-6 / 10)
    assert "FLAT_S" not in sel.weights and "FLAT_S" not in sel.scores
    assert aggregate_votes(pd.DataFrame()).weights == {}


# ------------------------------------------------------------- group 4: signals


def _bare_strategy(ctx) -> Top10VoteComboRunnerStrategy:
    """object.__new__ instance (no __init__ side effects), attrs set manually."""
    strategy = object.__new__(Top10VoteComboRunnerStrategy)
    strategy.alpha_id = "top10-vote-combo"
    strategy.version = "1"
    strategy.params = {}
    strategy.exchange = "binance"
    strategy.capital = 10_000.0
    strategy.leverage = 1.0
    strategy.fee_pct = 0.0007
    strategy.warmup_bars = DEFAULT_WARMUP_BARS
    strategy.retain_bars = DEFAULT_WARMUP_BARS
    strategy.scan_min_symbol_coverage = 0.9
    strategy._symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    strategy._symbol_set = set(strategy._symbols)
    strategy._last_processed_candle = 0
    strategy._open_positions = {}
    strategy.ctx = ctx
    return strategy


class _FakeCache:
    """Read-only stand-in for runner.data_layer.cache.SharedCandleCache."""

    def __init__(self, bars: dict[str, dict[str, tuple]]) -> None:
        self._bars = bars

    def snapshot(self, symbol, tf, bars):
        b = self._bars.get(symbol)
        if not b:
            return SimpleNamespace(
                opens=(), highs=(), lows=(), closes=(), volumes=(), times=()
            )
        n = len(b["times"])
        take = slice(max(0, n - bars), n) if bars and bars > 0 else slice(None)
        return SimpleNamespace(
            opens=tuple(b["opens"][take]),
            highs=tuple(b["highs"][take]),
            lows=tuple(b["lows"][take]),
            closes=tuple(b["closes"][take]),
            volumes=tuple(b["volumes"][take]),
            times=tuple(b["times"][take]),
        )

    def get_latest_timestamp(self, symbol, tf):
        b = self._bars.get(symbol)
        return b["times"][-1] if b and b["times"] else None

    def get_closes(self, symbol, tf, n=0):
        b = self._bars.get(symbol)
        if not b:
            return ()
        closes = b["closes"]
        return tuple(closes[-n:]) if n and n > 0 else tuple(closes)


def _make_ctx(cache) -> SimpleNamespace:
    ctx = SimpleNamespace(
        cache=cache,
        state=SimpleNamespace(ready=True),
        emit_signal=AsyncMock(return_value={"ok": True}),
        save_positions=lambda positions: ctx.saves.append(dict(positions)),
        clear_positions=lambda: ctx.clears.append(True),
        can_open_trades=lambda: True,
        price_alerts=SimpleNamespace(
            sync=lambda symbols: ctx.synced.append(set(symbols))
        ),
    )
    ctx.saves = []
    ctx.clears = []
    ctx.synced = []
    return ctx


def _signal_calls(ctx) -> list[tuple[str, dict]]:
    return [(c.args[0], c.kwargs) for c in ctx.emit_signal.await_args_list]


@pytest.mark.asyncio
async def test_apply_selection_closes_all_then_opens_basket() -> None:
    ctx = _make_ctx(_FakeCache({}))
    strategy = _bare_strategy(ctx)
    strategy._open_positions = {
        "OLDUSDT": {
            "position_id": "p-old",
            "symbol": "OLDUSDT",
            "side": "LONG",
            "entry": 50.0,
            "qty": 1.0,
        },
    }
    selection = VoteSelection(
        weights={"AAAUSDT": 0.6, "BBBUSDT": -0.4},
        vote_sums={"AAAUSDT": 6, "BBBUSDT": -4, "OLDUSDT": 2},
        up_votes={"AAAUSDT": 8, "BBBUSDT": 3, "OLDUSDT": 6},
        down_votes={"AAAUSDT": 2, "BBBUSDT": 7, "OLDUSDT": 4},
        scores={"AAAUSDT": 0.6, "BBBUSDT": -0.4},
    )
    payload = SelectionPayload(
        selection=selection,
        entry_prices={"AAAUSDT": 100.0, "BBBUSDT": 50.0},
        exit_prices={"OLDUSDT": 77.0},
    )
    await strategy._apply_selection(payload, BASE_MS)

    calls = _signal_calls(ctx)
    assert [kind for kind, _ in calls] == ["CLOSE", "OPEN", "OPEN"]

    _, close_kw = calls[0]
    assert close_kw["reason"] == "REBALANCE"
    assert close_kw["position_id"] == "p-old"
    assert close_kw["symbol"] == "OLDUSDT"
    assert close_kw["exit_price"] == 77.0
    assert close_kw["tf"] == "1d"
    assert close_kw["signal_candle_open_ms"] == BASE_MS

    _, open_a = calls[1]
    assert open_a["symbol"] == "AAAUSDT" and open_a["side"] == "LONG"
    assert open_a["entry"] == 100.0
    # qty = capital * |weight| * leverage / entry = 10000 * 0.6 * 1 / 100
    assert open_a["qty"] == pytest.approx(60.0)
    assert open_a["leverage"] == 1
    assert open_a["fee_pct"] == pytest.approx(0.0007)
    assert open_a["tf"] == "1d" and open_a["exchange"] == "binance"
    assert open_a["signal_candle_open_ms"] == BASE_MS
    meta_a = json.loads(open_a["metadata"])
    assert meta_a["vote_sum"] == 6
    assert meta_a["strength"] == 6
    assert meta_a["up_votes"] == 8 and meta_a["down_votes"] == 2
    assert meta_a["score"] == pytest.approx(0.6)
    assert meta_a["weight"] == pytest.approx(0.6)

    _, open_b = calls[2]
    assert open_b["symbol"] == "BBBUSDT" and open_b["side"] == "SHORT"
    assert open_b["entry"] == 50.0
    assert open_b["qty"] == pytest.approx(10_000.0 * 0.4 / 50.0)

    assert ctx.saves and set(ctx.saves[0]) == {"AAAUSDT", "BBBUSDT"}
    assert ctx.clears == []
    assert ctx.synced[-1] == {"AAAUSDT", "BBBUSDT"}
    assert set(strategy._open_positions) == {"AAAUSDT", "BBBUSDT"}
    assert strategy._open_positions["AAAUSDT"]["position_id"] == open_a["position_id"]


@pytest.mark.asyncio
async def test_apply_selection_empty_basket_closes_and_clears() -> None:
    ctx = _make_ctx(_FakeCache({}))
    strategy = _bare_strategy(ctx)
    strategy._open_positions = {
        "OLDUSDT": {
            "position_id": "p-old",
            "symbol": "OLDUSDT",
            "side": "SHORT",
            "entry": 40.0,
            "qty": 1.0,
        },
    }
    payload = SelectionPayload(
        selection=VoteSelection({}, {}, {}, {}, {}),
        entry_prices={},
        exit_prices={"OLDUSDT": 41.0},
    )
    await strategy._apply_selection(payload, BASE_MS)

    calls = _signal_calls(ctx)
    assert [kind for kind, _ in calls] == ["CLOSE"]
    assert calls[0][1]["reason"] == "REBALANCE"
    assert calls[0][1]["exit_price"] == 41.0
    assert ctx.saves == [] and ctx.clears == [True]
    assert strategy._open_positions == {}
    assert ctx.synced[-1] == set()


@pytest.mark.asyncio
async def test_apply_selection_skips_symbols_without_fill_price() -> None:
    ctx = _make_ctx(_FakeCache({}))
    strategy = _bare_strategy(ctx)
    payload = SelectionPayload(
        selection=VoteSelection(
            weights={"AAAUSDT": 1.0},
            vote_sums={"AAAUSDT": 5},
            up_votes={"AAAUSDT": 7},
            down_votes={"AAAUSDT": 2},
            scores={"AAAUSDT": 0.5},
        ),
        entry_prices={},
        exit_prices={},
    )
    await strategy._apply_selection(payload, BASE_MS)
    assert [kind for kind, _ in _signal_calls(ctx)] == []
    assert ctx.clears == [True]  # nothing opened -> positions cleared
    assert strategy._open_positions == {}


@pytest.mark.asyncio
async def test_apply_selection_none_payload_emits_nothing() -> None:
    ctx = _make_ctx(_FakeCache({}))
    strategy = _bare_strategy(ctx)
    strategy._open_positions = {
        "AAAUSDT": {
            "position_id": "p1",
            "symbol": "AAAUSDT",
            "entry": 1.0,
            "qty": 1.0,
        }
    }
    await strategy._apply_selection(None, BASE_MS)
    assert ctx.emit_signal.await_count == 0
    assert len(strategy._open_positions) == 1  # untouched on insufficient data


@pytest.mark.asyncio
async def test_manage_positions_syncs_price_alerts() -> None:
    ctx = _make_ctx(_FakeCache({}))
    strategy = _bare_strategy(ctx)
    strategy._open_positions = {
        "AAAUSDT": {"position_id": "p", "symbol": "AAAUSDT", "entry": 1.0, "qty": 1.0}
    }
    await strategy.manage_positions()
    assert ctx.synced[-1] == {"AAAUSDT"}


# ------------------------------------------------------------- group 5: wiring


def test_required_channels_is_1d_kline() -> None:
    assert Top10VoteComboRunnerStrategy.get_required_channels({}) == ["kline:1d"]


def test_instance_channels_include_symbols_broadcast() -> None:
    strategy = object.__new__(Top10VoteComboRunnerStrategy)
    strategy.params = {}
    strategy.exchange = "binance"
    assert strategy.get_required_channels_instance() == ["kline:1d", "symbols:binance"]


def test_warmup_wiring_from_whitelist_file() -> None:
    strategy = object.__new__(Top10VoteComboRunnerStrategy)
    strategy.alpha_id = "top10-vote-combo"
    strategy.params = {"whitelist_file": "top10-vote-combo/whitelist.txt"}
    strategy._alphas_root = ALPHAS_ROOT
    strategy.warmup_bars = DEFAULT_WARMUP_BARS
    strategy.retain_bars = DEFAULT_WARMUP_BARS
    strategy._symbols = strategy._load_universe()

    assert len(strategy._symbols) == 54
    assert strategy._symbols == sorted(strategy._symbols)
    assert "BTCUSDT" in strategy._symbols  # required by the residual factor
    assert strategy.get_warmup_symbols() == strategy._symbols
    assert strategy.get_warmup_tfs() == ["1d"]
    assert strategy.get_warmup_bars("1d") == 260
    assert strategy.get_retain_bars("1d") == 260
    strategy.warmup_bars = 300  # params-driven
    assert strategy.get_warmup_bars("1d") == 300
    assert strategy.get_retain_bars("1d") == 300


def test_missing_whitelist_param_raises() -> None:
    strategy = object.__new__(Top10VoteComboRunnerStrategy)
    strategy.alpha_id = "top10-vote-combo"
    strategy.params = {}
    strategy._alphas_root = ALPHAS_ROOT
    with pytest.raises(ValueError, match="whitelist_file"):
        strategy._load_universe()


def _bars_ending_at(last_open_ms: int, n: int = 6) -> dict[str, tuple]:
    return {
        "opens": tuple(100.0 for _ in range(n)),
        "highs": tuple(110.0 for _ in range(n)),
        "lows": tuple(90.0 for _ in range(n)),
        "closes": tuple(105.0 for _ in range(n)),
        "volumes": tuple(1000.0 for _ in range(n)),
        "times": tuple(last_open_ms - (n - 1 - j) * D1_MS for j in range(n)),
    }


def test_should_scan_coverage_gate_and_position_bypass() -> None:
    t_new = BASE_MS + 5 * D1_MS
    cache = _FakeCache(
        {
            "AAAUSDT": _bars_ending_at(t_new),
            "BBBUSDT": _bars_ending_at(t_new - D1_MS),
            "CCCUSDT": _bars_ending_at(t_new - D1_MS),
        }
    )
    strategy = _bare_strategy(_make_ctx(cache))
    assert strategy._last_processed_candle == 0
    # Coverage 1/3 < 0.9 and flat -> gated out
    assert strategy.should_scan_after_event("kline", "AAAUSDT", "1d") is False
    # Holding positions bypasses the coverage gate (2026-08-21 incident fix)
    strategy._open_positions = {
        "AAAUSDT": {
            "position_id": "p1",
            "symbol": "AAAUSDT",
            "entry": 100.0,
            "qty": 1.0,
        }
    }
    assert strategy.should_scan_after_event("kline", "AAAUSDT", "1d") is True


def test_should_scan_rejects_wrong_kind_tf_symbol_and_stale_candle() -> None:
    t_new = BASE_MS + 5 * D1_MS
    cache = _FakeCache({"AAAUSDT": _bars_ending_at(t_new)})
    strategy = _bare_strategy(_make_ctx(cache))
    assert strategy.should_scan_after_event("kline", "ZZZUSDT", "1d") is False
    assert strategy.should_scan_after_event("kline", "AAAUSDT", "1h") is False
    assert strategy.should_scan_after_event("price_alert", "AAAUSDT", "1d") is False
    assert strategy.should_scan_after_event("kline", None, "1d") is False
    strategy._last_processed_candle = t_new  # already processed this candle
    assert strategy.should_scan_after_event("kline", "AAAUSDT", "1d") is False


# ------------------------------------------------------------- group 6: registry


def test_registry_registers_strategy() -> None:
    from runner.strategy.registry import StrategyRegistry

    from runner.strategies.top10_vote_combo import register

    registry = StrategyRegistry()
    register(registry)
    assert "top10_vote_combo" in registry.names()
    assert registry.get_class("top10_vote_combo") is Top10VoteComboRunnerStrategy


# ------------------------------------------------- group 7: no-data / full scan


@pytest.mark.asyncio
async def test_scan_with_no_data_emits_nothing() -> None:
    ctx = _make_ctx(_FakeCache({}))
    strategy = _bare_strategy(ctx)
    await strategy.scan()
    assert ctx.emit_signal.await_count == 0
    assert ctx.saves == [] and ctx.clears == []
    assert strategy._last_processed_candle == 0


@pytest.mark.asyncio
async def test_scan_with_insufficient_bars_emits_nothing() -> None:
    n = 10  # 9 closed bars < MIN_CLOSED_BARS
    assert n - 1 < MIN_CLOSED_BARS
    symbols = ("BTCUSDT", "AAAUSDT", "BBBUSDT")
    bars = {}
    for i, symbol in enumerate(symbols):
        scale = 100.0 + i
        closes = tuple(scale + 0.01 * j for j in range(n))
        opens = (closes[0],) + closes[:-1]
        bars[symbol] = {
            "opens": opens,
            "highs": tuple(max(o, c) * 1.01 for o, c in zip(opens, closes)),
            "lows": tuple(min(o, c) * 0.99 for o, c in zip(opens, closes)),
            "closes": closes,
            "volumes": tuple(1000.0 for _ in range(n)),
            "times": tuple(BASE_MS + j * D1_MS for j in range(n)),
        }
    ctx = _make_ctx(_FakeCache(bars))
    strategy = _bare_strategy(ctx)
    strategy._symbols = list(symbols)
    strategy._symbol_set = set(symbols)
    await strategy.scan()
    assert ctx.emit_signal.await_count == 0
    # The bar is still consumed so it is not re-scanned on every event
    assert strategy._last_processed_candle == BASE_MS + (n - 1) * D1_MS


UNIVERSE = ("BTCUSDT", "S1USDT", "S2USDT", "S3USDT", "S4USDT", "S5USDT", "S6USDT")
N_CLOSED = 260


def _diverse_bars(seed: int) -> dict[str, tuple]:
    """Deterministic random-walk OHLCV + one forming candle at the end."""
    rng = np.random.default_rng(seed)
    closes = [100.0]
    for _ in range(N_CLOSED - 1):
        closes.append(closes[-1] * (1.0 + rng.normal(0.0, 0.02)))
    closes_t = tuple(closes)
    opens_t = (closes_t[0],) + closes_t[:-1]
    highs_t = tuple(
        max(o, c) * (1.0 + rng.uniform(0.001, 0.02)) for o, c in zip(opens_t, closes_t)
    )
    lows_t = tuple(
        min(o, c) * (1.0 - rng.uniform(0.001, 0.02)) for o, c in zip(opens_t, closes_t)
    )
    volumes_t = tuple(float(rng.uniform(500.0, 5000.0)) for _ in range(N_CLOSED))
    times_t = tuple(BASE_MS + j * D1_MS for j in range(N_CLOSED))
    last_close = closes_t[-1]
    return {
        "opens": opens_t + (last_close,),
        "highs": highs_t + (last_close * 1.002,),
        "lows": lows_t + (last_close * 0.998,),
        "closes": closes_t + (last_close * 1.001,),
        "volumes": volumes_t + (1234.0,),
        "times": times_t + (BASE_MS + N_CLOSED * D1_MS,),
    }


@pytest.mark.asyncio
async def test_scan_full_pipeline_closes_held_then_opens_basket() -> None:
    cache = _FakeCache(
        {symbol: _diverse_bars(100 + i) for i, symbol in enumerate(UNIVERSE)}
    )
    latest = BASE_MS + N_CLOSED * D1_MS
    ctx = _make_ctx(cache)
    strategy = _bare_strategy(ctx)
    strategy._symbols = list(UNIVERSE)
    strategy._symbol_set = set(UNIVERSE)
    strategy._open_positions = {
        "OLDUSDT": {
            "position_id": "old-1",
            "symbol": "OLDUSDT",
            "side": "LONG",
            "entry": 50.0,
            "qty": 2.0,
        }
    }
    # Coverage 7/7 -> gate passes flat
    assert strategy.should_scan_after_event("kline", "BTCUSDT", "1d") is True

    await strategy.scan()

    assert strategy._last_processed_candle == latest
    calls = _signal_calls(ctx)
    assert calls, "expected signals from the full pipeline"
    assert calls[0][0] == "CLOSE"
    assert calls[0][1]["reason"] == "REBALANCE"
    assert calls[0][1]["position_id"] == "old-1"

    opens = [kw for kind, kw in calls if kind == "OPEN"]
    assert opens, "expected OPEN signals from non-degenerate data"
    assert all(kw["symbol"] in set(UNIVERSE) for kw in opens)
    total_abs_weight = 0.0
    for kw in opens:
        assert kw["tf"] == "1d" and kw["leverage"] == 1
        assert kw["fee_pct"] == pytest.approx(0.0007)
        assert kw["exchange"] == "binance"
        assert kw["side"] in ("LONG", "SHORT")
        assert kw["entry"] > 0 and kw["qty"] > 0
        assert isinstance(kw["position_id"], str) and kw["position_id"]
        assert kw["signal_candle_open_ms"] == latest
        meta = json.loads(kw["metadata"])
        assert {
            "vote_sum",
            "strength",
            "up_votes",
            "down_votes",
            "score",
            "weight",
        } <= set(meta)
        assert meta["strength"] == abs(meta["vote_sum"])
        assert meta["up_votes"] - meta["down_votes"] == meta["vote_sum"]
        assert (meta["vote_sum"] > 0) == (kw["side"] == "LONG")
        assert kw["qty"] == pytest.approx(
            strategy.capital * abs(meta["weight"]) * strategy.leverage / kw["entry"]
        )
        total_abs_weight += abs(meta["weight"])
    assert total_abs_weight == pytest.approx(1.0)
    assert ctx.saves and set(ctx.saves[0]) == {kw["symbol"] for kw in opens}
    assert ctx.synced[-1] == set(ctx.saves[0])
