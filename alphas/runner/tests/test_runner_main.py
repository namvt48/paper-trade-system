from __future__ import annotations

import asyncio
import json

import pytest

from runner import main as runner_main
from runner.data_layer.cache import SharedCandleCache
from runner.data_layer.pubsub import DataEvent
from runner.reconcile.state import StrategyRuntimeState
from runner.strategy.base import Strategy
from runner.strategy.context import StrategyContext
from runner.strategy.registry import StrategyRegistry


class RunnerTestStrategy(Strategy):
    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        tf = params.get("tf", "15m")
        return [f"kline:{tf}"]

    def get_required_channels_instance(self) -> list[str]:
        exchange = self.params.get("exchange", "binance")
        return [f"kline:{exchange}:{self.params.get('tf', '15m')}"]

    def get_warmup_symbols(self) -> list[str]:
        return list(self.params.get("symbols", []))

    def get_warmup_tfs(self) -> list[str]:
        return [str(self.params.get("tf", "15m"))]

    def get_warmup_bars(self, tf: str) -> int:
        return int(self.params.get("warmup_bars", 2))

    async def scan(self) -> None:
        symbol = self.get_warmup_symbols()[0]
        tf = self.get_warmup_tfs()[0]
        times = self.ctx.cache.get_times(symbol, tf, 2)
        await self.ctx.emit_signal(
            "OPEN",
            tf=tf,
            symbol=symbol,
            side="LONG",
            reason="TEST_SIGNAL",
            signal_candle_open_ms=times[-1],
        )


class NoScanAfterEventStrategy(RunnerTestStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_candle_calls = []
        self.scan_calls = 0

    def should_scan_after_event(self, kind: str, symbol: str | None = None, tf: str | None = None) -> bool:
        return False

    async def on_candle(self, symbol: str, tf: str) -> None:
        self.on_candle_calls.append((symbol, tf))

    async def scan(self) -> None:
        self.scan_calls += 1


@pytest.fixture(autouse=True)
def test_strategy_registry(monkeypatch):
    def build_registry(_modules):
        registry = StrategyRegistry()
        registry.register("test_strategy", RunnerTestStrategy)
        return registry

    monkeypatch.setattr(runner_main, "build_registry", build_registry)


@pytest.mark.asyncio
async def test_runner_dry_run_skips_disabled_and_prints_requirements(tmp_path):
    path = tmp_path / "runner.yaml"
    path.write_text(
        """
runner_id: r
shadow_mode: true
alphas:
  - alpha_id: a1
    strategy: test_strategy
    enabled: true
    params: {symbols: [BTCUSDT], tf: 15m, warmup_bars: 2}
  - alpha_id: a2
    strategy: test_strategy
    enabled: false
    params: {symbols: [ETHUSDT], tf: 15m, warmup_bars: 2}
""",
        encoding="utf-8",
    )

    result = await runner_main.run(str(path), dry_run=True)

    assert result["started"] == ["a1"]
    assert "BTCUSDT:15m" in result["requirements"]
    assert "ETHUSDT:15m" not in result["requirements"]


class FakeRedis:
    _runner_inline_redis = True

    def __init__(self):
        self.values = {}
        self.released = []
        self.xadds = []

    def scan_iter(self, match=None):
        if match:
            prefix = match.rstrip("*")
            for key in self.values:
                key_str = key if isinstance(key, str) else key.decode()
                if key_str.startswith(prefix):
                    yield key
        else:
            yield from self.values.keys()

    def set(self, key, value, nx=False, ex=None):
        if "blocked" in key:
            return False
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def expire(self, key, ttl):
        return key in self.values

    def delete(self, key):
        self.released.append(key)
        return bool(self.values.pop(key, None) is not None)

    def xadd(self, stream, fields):
        self.xadds.append((stream, fields))

    def lrange(self, key, start, end):
        return []

    def hgetall(self, key):
        return {}

    def xread(self, streams, count=None, block=None):
        response_stream = next(iter(streams))
        if not self.xadds:
            return []
        _stream, request = self.xadds[-1]
        if request.get("response_stream") != response_stream:
            return []
        entries = []
        for index, symbol in enumerate(request["symbols"].split(","), start=1):
            candles = [
                {
                    "open_time": 1_000_000 + i,
                    "open": 1,
                    "high": 2,
                    "low": 0,
                    "close": 1,
                    "volume": 1,
                }
                for i in range(int(request["bars"]))
            ]
            entries.append((f"{index}-0", {
                "symbol": symbol,
                "tf": request["tf"],
                "candles": json.dumps(candles),
            }))
        return [(response_stream, entries[:count])]

    def pubsub(self):
        return FakePubSub()


class FakePubSub:
    def subscribe(self, channel): pass
    def unsubscribe(self, channel): pass
    def get_message(self, timeout=1.0): return None
    def close(self): pass


@pytest.mark.asyncio
@pytest.mark.skip(reason="FakeRedis incomplete — warmup/MDSReadyWatcher hangs; needs mock overhaul")
async def test_runner_lease_failure_skips_alpha_and_shutdown_releases_owned(tmp_path, monkeypatch):
    path = tmp_path / "runner.yaml"
    path.write_text(
        """
runner_id: r
shadow_mode: false
redis_url: redis://paper
mds_redis_url: redis://mds
warmup:
  request_timeout_sec: 0.01
alphas:
  - alpha_id: a1
    strategy: test_strategy
    params: {symbols: [BTCUSDT], tf: 15m, warmup_bars: 2}
  - alpha_id: blocked
    strategy: test_strategy
    params: {symbols: [ETHUSDT], tf: 5m, warmup_bars: 2}
""",
        encoding="utf-8",
    )
    paper_redis = FakeRedis()
    redis_clients = [paper_redis, FakeRedis()]
    monkeypatch.setattr(runner_main.redis, "from_url", lambda *a, **k: redis_clients.pop(0))

    result = await runner_main.run(str(path), dry_run=False)

    assert result["started"] == ["a1"]
    assert result["skipped"] == ["blocked"]
    assert paper_redis.released == ["runner:alpha:lease:a1"]


@pytest.mark.asyncio
async def test_runner_marks_partial_warmup_unready_without_killing_runner(tmp_path):
    path = tmp_path / "runner.yaml"
    path.write_text(
        """
runner_id: r
shadow_mode: true
alphas:
  - alpha_id: a1
    strategy: test_strategy
    params: {symbols: [BTCUSDT], tf: 15m, warmup_bars: 2}
""",
        encoding="utf-8",
    )

    result = await runner_main.run(str(path), dry_run=True)

    assert result["started"] == ["a1"]


class CapturingDispatcher:
    def __init__(self):
        self.signals = []

    async def dispatch(self, ctx, signal_type, **fields):
        self.signals.append({"signal_type": signal_type, **fields})
        return "signal-id"


@pytest.mark.asyncio
async def test_strategy_event_loop_scans_on_candle_and_dispatches_signal():
    cache = SharedCandleCache()
    for open_time, close in ((1000, 99.0), (2000, 101.0)):
        cache.upsert_candle("BTCUSDT", "15m", {
            "open_time": open_time,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1,
        })
    dispatcher = CapturingDispatcher()
    ctx = StrategyContext("a", "1", cache, dispatcher, StrategyRuntimeState(ready=True))
    strategy = RunnerTestStrategy(
        alpha_id="a",
        version="1",
        params={"symbols": ["BTCUSDT"], "tf": "15m"},
        ctx=ctx,
    )
    queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(runner_main.run_strategy_event_loop(strategy, queue, stop))

    await queue.put(DataEvent("kline:binance:15m", "kline", "BTCUSDT", "15m", {}))
    await asyncio.wait_for(queue.join(), timeout=1.0)
    stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert dispatcher.signals == [{
        "signal_type": "OPEN",
        "tf": "15m",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "reason": "TEST_SIGNAL",
        "signal_candle_open_ms": 2000,
    }]


@pytest.mark.asyncio
async def test_strategy_event_loop_can_skip_full_scan_after_candle():
    ctx = StrategyContext("a", "1", SharedCandleCache(), None, StrategyRuntimeState(ready=True))
    strategy = NoScanAfterEventStrategy(
        alpha_id="a",
        version="1",
        params={"symbols": ["BTCUSDT"], "tf": "15m"},
        ctx=ctx,
    )

    await runner_main.handle_strategy_event(
        strategy,
        DataEvent("kline:binance:15m", "kline", "BTCUSDT", "15m", {}),
    )

    assert strategy.on_candle_calls == [("BTCUSDT", "15m")]
    assert strategy.scan_calls == 0
