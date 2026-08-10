import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from runner.strategies.vn30_tcbs.logic import Alpha21AlmaCross, alpha19_decision
from runner.strategies.vn30_tcbs.strategy import Vn30TcbsRunnerStrategy
from runner.config import load_runner_config
from runner.main import build_registry


def _alpha19_fixture() -> tuple[list[float], list[float], list[float]]:
    rng = random.Random(2)
    closes = [1800.0]
    highs: list[float] = []
    lows: list[float] = []
    for index in range(220):
        if index:
            closes.append(closes[-1] + rng.uniform(-8.0, 8.0))
        highs.append(closes[-1] + rng.uniform(0.1, 5.0))
        lows.append(closes[-1] - rng.uniform(0.1, 5.0))
    return highs, lows, closes


def test_alpha21_does_not_reopen_from_a_stale_condition_after_forced_flat() -> None:
    alpha = Alpha21AlmaCross(period=3, sigma=1.0, threshold_bps=25.0)

    for close in (100.0, 100.0, 100.0, 103.0):
        alpha.on_bar(close)
    assert alpha.side == 1

    alpha.force_flat()

    # 101.5 is inside the current ALMA band, so this bar has no fresh signal.
    assert alpha.on_bar(101.5) == 0
    assert alpha.condition == 0


def test_alpha21_uses_exactly_period_bars_and_holds_side_through_neutral_zone() -> None:
    alpha = Alpha21AlmaCross(period=3, sigma=1.0, threshold_bps=25.0)

    assert alpha.on_bar(100.0) == 0
    assert alpha.on_bar(100.0) == 0
    assert alpha.on_bar(103.0) == 1

    assert alpha.on_bar(101.5) == 1
    assert alpha.condition == 0


def test_alpha19_matches_supplied_short_condition_and_cut_level() -> None:
    highs, lows, closes = _alpha19_fixture()

    decision = alpha19_decision(highs, lows, closes)

    assert decision.condition == -1
    assert decision.side == -1
    assert decision.uo is not None and decision.uo < 50.0
    assert decision.stochastic_rank == 0.0
    assert decision.cut_loss == 1810.0


def test_alpha19_holds_safely_when_true_range_is_zero() -> None:
    values = [1800.0] * 220

    decision = alpha19_decision(values, values, values, current_side=1, cut_loss=1790.0)

    assert decision.side == 1
    assert decision.condition == 0
    assert decision.uo is None


def _strategy_context() -> SimpleNamespace:
    return SimpleNamespace(
        load_authoritative_positions=lambda: {},
        load_positions=lambda: {},
        save_positions=MagicMock(),
        clear_positions=MagicMock(),
        emit_signal=AsyncMock(return_value={"ok": True}),
        can_open_trades=lambda: True,
    )


@pytest.mark.asyncio
async def test_vn30_tcbs_open_signal_uses_tcbs_symbol_and_persists_cut_level() -> None:
    ctx = _strategy_context()
    strategy = Vn30TcbsRunnerStrategy(
        "vn30-alpha-19",
        "1",
        {"preset": 19, "symbol": "41I1G8000", "exchange": "tcbs", "timeframe": "5m"},
        ctx,
    )

    await strategy._transition_to_side(
        1,
        1875.0,
        123_000,
        reason="ALPHA_SIGNAL",
        cut_loss=1880.0,
    )

    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    assert call.kwargs["symbol"] == "41I1G8000"
    assert call.kwargs["exchange"] == "tcbs"
    assert call.kwargs["side"] == "LONG"
    assert call.kwargs["qty"] == 1.0
    assert call.kwargs["tf"] == "5m"
    assert strategy._current_cut_loss() == 1880.0
    ctx.save_positions.assert_called_once()


@pytest.mark.asyncio
async def test_vn30_tcbs_reverse_flips_alpha19_open_side() -> None:
    highs, lows, closes = _alpha19_fixture()
    ctx = _strategy_context()
    ctx.cache = SimpleNamespace(
        snapshot=lambda symbol, tf, bars: SimpleNamespace(
            times=list(range(len(closes))), highs=highs, lows=lows, closes=closes
        )
    )
    ctx.state = SimpleNamespace(ready=True)

    strategy = Vn30TcbsRunnerStrategy(
        "vn30-alpha-19-reverse",
        "1",
        {
            "preset": 19,
            "symbol": "41I1G8000",
            "exchange": "tcbs",
            "timeframe": "5m",
            "reverse": True,
        },
        ctx,
    )
    strategy._must_force_flat = lambda candle_open: False
    strategy._inside_trade_session = lambda candle_open: True
    strategy._pending_candle_open = len(closes) - 1

    await strategy.scan()

    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    # The non-reverse alpha opens SHORT on this fixture (see
    # test_alpha19_matches_supplied_short_condition_and_cut_level); reverse
    # must mirror it to LONG.
    assert call.kwargs["side"] == "LONG"


@pytest.mark.asyncio
async def test_vn30_tcbs_reverse_flips_alpha21_open_side() -> None:
    closes = [100.0, 100.0, 100.0, 103.0]
    ctx = _strategy_context()
    ctx.cache = SimpleNamespace(
        snapshot=lambda symbol, tf, bars: SimpleNamespace(
            times=list(range(len(closes))), highs=closes, lows=closes, closes=closes
        )
    )
    ctx.state = SimpleNamespace(ready=True)

    strategy = Vn30TcbsRunnerStrategy(
        "vn30-alpha-21-reverse",
        "1",
        {
            "preset": 21,
            "symbol": "41I1G8000",
            "exchange": "tcbs",
            "timeframe": "5m",
            "period": 3,
            "sigma": 1.0,
            "threshold_bps": 25.0,
            "reverse": True,
        },
        ctx,
    )
    strategy._must_force_flat = lambda candle_open: False
    strategy._inside_trade_session = lambda candle_open: True
    strategy._pending_candle_open = len(closes) - 1

    await strategy.scan()

    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    # The non-reverse alpha opens LONG on this sequence (see
    # test_alpha21_uses_exactly_period_bars_and_holds_side_through_neutral_zone);
    # reverse must mirror it to SHORT.
    assert call.kwargs["side"] == "SHORT"


def test_vn30_tcbs_runner_requests_five_minute_tcbs_candles() -> None:
    assert Vn30TcbsRunnerStrategy.get_required_channels(
        {"preset": 21, "timeframe": "5m"}
    ) == ["kline:5m"]


def test_tcbs_runner_config_enables_only_supplied_alpha_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "RUNNER_ID",
        "REDIS_URL",
        "MDS_REDIS_URL",
        "MDS_EXCHANGE",
        "MAX_ALPHAS_PER_RUNNER",
    ):
        monkeypatch.delenv(variable, raising=False)
    root = Path(__file__).resolve().parents[3]
    config = load_runner_config(root / "runner-config-tcbs.production.yaml")
    registry = build_registry(config.modules)

    assert config.runner_id == "paper-runner-tcbs"
    assert config.mds_exchange == "tcbs"
    assert {alpha.alpha_id for alpha in config.alphas} == {
        "vn30-alpha-19",
        "vn30-alpha-21",
        "vn30-alpha-19-reverse",
        "vn30-alpha-21-reverse",
    }
    assert all(alpha.enabled for alpha in config.alphas)
    reverse_alphas = {
        alpha.alpha_id: alpha
        for alpha in config.alphas
        if alpha.alpha_id.endswith("-reverse")
    }
    assert reverse_alphas["vn30-alpha-19-reverse"].params["reverse"] is True
    assert reverse_alphas["vn30-alpha-21-reverse"].params["reverse"] is True
    assert registry.get_class("vn30_tcbs") is Vn30TcbsRunnerStrategy
