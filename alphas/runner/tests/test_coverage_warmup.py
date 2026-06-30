from __future__ import annotations

from dataclasses import dataclass

from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState
from runner.strategy.context import StrategyContext
from runner.strategies.cross_sectional.strategy import CrossSectionalRunnerStrategy


TF_MS_15M = 15 * 60 * 1000


@dataclass(frozen=True)
class FakeSpec:
    alpha_id: str = "test-alpha"
    timeframe: str = "15m"
    signal: str = "close"
    params: dict = None
    universe_size: int = 20
    universe_mode: str = "fixed"
    rebalance_bars: int = 1
    exec_lag: int = 1
    vol_lookback: int = 20
    ppy: int = 4
    long_threshold: float = None
    short_threshold: float = None
    required_bars: int = 10

    def __post_init__(self):
        if self.params is None:
            object.__setattr__(self, "params", {})


def _make_strategy(
    symbols: list[str],
    cache: SharedCandleCache,
    warmup_coverage: float = 0.95,
) -> CrossSectionalRunnerStrategy:
    state = StrategyRuntimeState(ready=True)
    ctx = StrategyContext("test-alpha", "1", cache, None, state, warmup_coverage)
    params = {
        "spec_file": "15m-blend-close/spec.json",
        "universe_file": "15m-blend-close/data/universe.json",
        "blacklist_file": "15m-blend-close/blacklist.txt",
        "warmup_bars": 10,
        "retain_bars": 10,
        "capital": 10000.0,
        "exchange": "binance",
        "offset_candle_sec": 5.0,
    }
    strategy = CrossSectionalRunnerStrategy.__new__(CrossSectionalRunnerStrategy)
    strategy.alpha_id = "test-alpha"
    strategy.version = "1"
    strategy.params = params
    strategy.ctx = ctx
    strategy.spec = FakeSpec()
    strategy._symbols = list(symbols)
    strategy._symbol_set = set(symbols)
    strategy.scan_min_symbol_coverage = warmup_coverage
    strategy._last_processed_candle = 0
    strategy._pending = None
    strategy._open_positions = {}
    strategy._portfolio_returns = []
    strategy._last_prices = {}
    strategy._base_weights = {}
    strategy._pending_cost = 0.0
    strategy._strategy_leverage = 0.0
    strategy._last_pnl_publish = {}
    strategy._pnl_channel = "pnl:test-alpha"
    strategy._warmup_complete = False
    from runner.shared_panel_feature_cache import SharedPanelFeatureCache
    if ctx.panel_feature_cache is None:
        ctx.panel_feature_cache = SharedPanelFeatureCache()
    return strategy


def _insert_candle(cache: SharedCandleCache, symbol: str, tf: str, open_time: int):
    cache.upsert_candle(symbol, tf, {
        "open_time": open_time,
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 100.0,
    })


class TestCandleCoverageAfterWarmup:
    def test_before_warmup_complete_coverage_check_required(self):
        cache = SharedCandleCache()
        symbols = [f"S{i}" for i in range(20)]
        for s in symbols:
            _insert_candle(cache, s, "15m", 1000)

        strategy = _make_strategy(symbols, cache, warmup_coverage=0.95)
        strategy._last_processed_candle = 1000
        strategy._warmup_complete = False

        only_one_symbol_advanced = 2000
        _insert_candle(cache, symbols[0], "15m", only_one_symbol_advanced)

        result = strategy.should_scan_after_event("kline", symbols[0], "15m")
        assert result is False, "Before warmup complete, coverage < 0.95 should block scan"

    def test_after_warmup_complete_single_symbol_advance_allows_scan(self):
        cache = SharedCandleCache()
        symbols = [f"S{i}" for i in range(20)]
        for s in symbols:
            _insert_candle(cache, s, "15m", 1000)

        strategy = _make_strategy(symbols, cache, warmup_coverage=0.95)
        strategy._last_processed_candle = 1000
        strategy._warmup_complete = True

        next_candle = 2000
        # Advance all symbols — coverage must be met even after warmup
        for s in symbols:
            _insert_candle(cache, s, "15m", next_candle)

        result = strategy.should_scan_after_event("kline", symbols[0], "15m")
        assert result is True, "After warmup complete, full coverage advance should allow scan"

    def test_after_warmup_complete_stale_event_rejected(self):
        cache = SharedCandleCache()
        symbols = [f"S{i}" for i in range(20)]
        for s in symbols:
            _insert_candle(cache, s, "15m", 1000)

        strategy = _make_strategy(symbols, cache, warmup_coverage=0.95)
        strategy._last_processed_candle = 2000
        strategy._warmup_complete = True

        result = strategy.should_scan_after_event("kline", symbols[0], "15m")
        assert result is False, "Stale event (candle_open_ms <= last_processed) should be rejected"

    def test_large_gap_resets_warmup_complete(self):
        cache = SharedCandleCache()
        symbols = [f"S{i}" for i in range(20)]
        for s in symbols:
            _insert_candle(cache, s, "15m", 1000)

        strategy = _make_strategy(symbols, cache, warmup_coverage=0.95)
        strategy._last_processed_candle = 1000
        strategy._warmup_complete = True

        huge_gap_time = 1000 + TF_MS_15M * 10
        _insert_candle(cache, symbols[0], "15m", huge_gap_time)

        result = strategy.should_scan_after_event("kline", symbols[0], "15m")
        assert result is False, "Large gap (>5 candles) should reset warmup_complete and re-require coverage"

    def test_small_gap_keeps_warmup_complete(self):
        cache = SharedCandleCache()
        symbols = [f"S{i}" for i in range(20)]
        for s in symbols:
            _insert_candle(cache, s, "15m", 1000)

        strategy = _make_strategy(symbols, cache, warmup_coverage=0.95)
        strategy._last_processed_candle = 1000
        strategy._warmup_complete = True

        next_candle = 1000 + TF_MS_15M
        # Advance all symbols to meet coverage requirement
        for s in symbols:
            _insert_candle(cache, s, "15m", next_candle)

        result = strategy.should_scan_after_event("kline", symbols[0], "15m")
        assert result is True, "Normal 1-candle advance with full coverage should keep warmup_complete"

    def test_wrong_tf_rejected(self):
        cache = SharedCandleCache()
        symbols = [f"S{i}" for i in range(20)]
        for s in symbols:
            _insert_candle(cache, s, "15m", 1000)

        strategy = _make_strategy(symbols, cache, warmup_coverage=0.95)
        strategy._last_processed_candle = 1000
        strategy._warmup_complete = True

        _insert_candle(cache, symbols[0], "1h", 2000)

        result = strategy.should_scan_after_event("kline", symbols[0], "1h")
        assert result is False, "Wrong TF should be rejected"
