from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from cross_alpha.spec import AlphaSpec
from indicators.pandas.ts_ops import ts_zscore
from runner.strategies.cross_sectional.strategy import CrossSectionalRunnerStrategy


class FakePipeline:
    def __init__(self, lists: dict):
        self._lists = lists
        self._calls: list[tuple] = []

    def lrange(self, key, start, end):
        self._calls.append((key, start, end))
        return self

    def execute(self):
        out = []
        for key, start, end in self._calls:
            values = self._lists.get(key, [])
            end_idx = len(values) - 1 if end == -1 else end
            out.append(values[start:end_idx + 1])
        return out


class FakeRedis:
    def __init__(self):
        self.lists = {}

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        if end == -1:
            end = len(values) - 1
        return values[start:end + 1]

    def pipeline(self, transaction=False):
        return FakePipeline(self.lists)


def _row(funding_time, funding_rate):
    return json.dumps({"funding_time": funding_time, "funding_rate": funding_rate})


def _spec(needs_funding: bool) -> AlphaSpec:
    return AlphaSpec(
        alpha_id="test-chmom",
        timeframe="1d",
        signal="carry_momentum",
        params={"momentum_window": 5, "funding_window": 10, "ema_span": 3},
        universe_size=2,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
        needs_funding=needs_funding,
    )


def _bare_strategy(spec: AlphaSpec, symbols: list[str], redis_client) -> CrossSectionalRunnerStrategy:
    s = CrossSectionalRunnerStrategy.__new__(CrossSectionalRunnerStrategy)
    s.spec = spec
    s.exchange = "binance"
    s._symbols = symbols
    s.ctx = SimpleNamespace(mds_redis_client=redis_client)
    return s


_EIGHT_HOURS_MS = 8 * 3600 * 1000


def test_attach_funding_panel_zscores_at_native_frequency_before_reindex():
    # 10 settlements at BTCUSDT's real native 8h cadence, feeding a
    # funding_window=10 zscore.
    rates = [0.0001, 0.0001, 0.0001, 0.0001, 0.0002, 0.0001, 0.0001, 0.0003, 0.0001, 0.0005]
    times = [i * _EIGHT_HOURS_MS for i in range(10)]
    redis = FakeRedis()
    redis.lists["funding_snapshot:binance:BTCUSDT"] = [_row(t, r) for t, r in zip(times, rates)]
    strategy = _bare_strategy(_spec(True), ["BTCUSDT"], redis)
    # "Daily" bars land many settlements apart -- if the zscore were (wrongly)
    # computed AFTER reindexing onto these 3 coarse bars, a window=10 there
    # would need 10 of these coarse bars' worth of settlements, not just 10
    # raw settlements as intended.
    close = pd.DataFrame(
        {"BTCUSDT": [1.0, 2.0, 3.0]},
        index=pd.Index([_EIGHT_HOURS_MS // 2, times[5] + 1, times[9] + 1], dtype="int64"),
    )
    panel = {"close": close}

    strategy._attach_funding_panel(panel)

    raw = pd.DataFrame({"BTCUSDT": rates}, index=pd.Index(times, dtype="int64"))
    expected = ts_zscore(raw, 10).reindex(close.index, method="ffill")
    assert "funding_zscore" in panel
    pd.testing.assert_frame_equal(panel["funding_zscore"], expected)


def test_attach_funding_panel_does_not_dilute_slower_symbol_via_faster_symbol():
    # BTCUSDT settles at its real native 8h cadence. ALTUSDT settles every
    # hour (8x more often) and shares the same funding panel. Before the
    # shared-8h-bucket resample, combining both into one DataFrame padded
    # BTCUSDT's window with ALTUSDT's extra timestamps -- starving a
    # funding_window=10 rolling window of real BTCUSDT values. It must not.
    btc_rates = [0.0001, 0.0001, 0.0001, 0.0001, 0.0002, 0.0001, 0.0001, 0.0003, 0.0001, 0.0005]
    btc_times = [i * _EIGHT_HOURS_MS for i in range(10)]
    alt_times = list(range(0, 10 * _EIGHT_HOURS_MS, _EIGHT_HOURS_MS // 8))
    alt_rates = [0.0002] * len(alt_times)

    redis = FakeRedis()
    redis.lists["funding_snapshot:binance:BTCUSDT"] = [_row(t, r) for t, r in zip(btc_times, btc_rates)]
    redis.lists["funding_snapshot:binance:ALTUSDT"] = [_row(t, r) for t, r in zip(alt_times, alt_rates)]
    strategy = _bare_strategy(_spec(True), ["BTCUSDT", "ALTUSDT"], redis)
    close = pd.DataFrame(
        {"BTCUSDT": [1.0, 2.0, 3.0], "ALTUSDT": [1.0, 2.0, 3.0]},
        index=pd.Index([_EIGHT_HOURS_MS // 2, btc_times[5] + 1, btc_times[9] + 1], dtype="int64"),
    )
    panel = {"close": close}

    strategy._attach_funding_panel(panel)

    raw = pd.DataFrame({"BTCUSDT": btc_rates}, index=pd.Index(btc_times, dtype="int64"))
    expected_btc = ts_zscore(raw, 10).reindex(close.index, method="ffill")["BTCUSDT"]
    pd.testing.assert_series_equal(panel["funding_zscore"]["BTCUSDT"], expected_btc, check_names=False)


def test_attach_funding_panel_skips_when_spec_does_not_need_it():
    redis = FakeRedis()
    redis.lists["funding_snapshot:binance:BTCUSDT"] = [_row(1_000, 0.0001)]
    strategy = _bare_strategy(_spec(False), ["BTCUSDT"], redis)
    panel = {"close": pd.DataFrame({"BTCUSDT": [1.0]}, index=pd.Index([1], dtype="int64"))}

    strategy._attach_funding_panel(panel)

    assert "funding_zscore" not in panel


def test_attach_funding_panel_noop_without_redis_client():
    strategy = _bare_strategy(_spec(True), ["BTCUSDT"], None)
    panel = {"close": pd.DataFrame({"BTCUSDT": [1.0]}, index=pd.Index([1], dtype="int64"))}

    strategy._attach_funding_panel(panel)

    assert "funding_zscore" not in panel


def test_attach_funding_panel_is_idempotent_per_bundle():
    redis = FakeRedis()
    redis.lists["funding_snapshot:binance:BTCUSDT"] = [_row(1_000, 0.0001)]
    strategy = _bare_strategy(_spec(True), ["BTCUSDT"], redis)
    panel = {"close": pd.DataFrame({"BTCUSDT": [1.0]}, index=pd.Index([1_000], dtype="int64"))}

    strategy._attach_funding_panel(panel)
    redis.lists["funding_snapshot:binance:BTCUSDT"] = []  # would now fail to (re)load
    strategy._attach_funding_panel(panel)  # second call must be a no-op, not clear/refetch

    assert "funding_zscore" in panel
