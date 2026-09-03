"""Regression tests for the 2026-07-16 incident: 5 daily cross-sectional
alphas (1d-kertrend, 1d-vwaprev, 1d-iamp, 1d-chmom, ensemble-1d) went
silent from 11:57 UTC onward and never processed another candle until
the runner was restarted, while 1d-trend60cmf kept working.

Root cause: a universe refresh (`symbols:binance`) triggered a scan() on
every alpha at once; funding-reading alphas (chmom, ensemble) read up to
180 symbols via synchronous `redis.lrange` with no timeout inside
`asyncio.to_thread`, competing for the runner's small shared thread
pool. `run_strategy_event_loop` awaits `handle_strategy_event` with no
timeout, so once a strategy's own scan() hangs, that strategy's loop
never processes another event -- not even next day's candle close --
until the process is restarted.

These tests reproduce the mechanism at unit-test speed (no real redis,
no real threads) and must fail on the current code (RED) before the
fix (U2 async+timeout funding reads, U4 watchdog) lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

from runner import main as runner_main
from runner.data_layer.cache import SharedCandleCache
from runner.data_layer.pubsub import DataEvent
from runner.data_layer.funding_snapshot import FundingSnapshotReader
from runner.reconcile.state import StrategyRuntimeState
from runner.strategy.base import Strategy
from runner.strategy.context import StrategyContext


class FakePipeline:
    """Minimal stand-in for redis-py's pipeline: queues lrange calls and
    returns their results in order on ``execute()``, matching the shape
    ``FundingSnapshotReader.load_many`` relies on."""

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
            out.append(values[start : end_idx + 1])
        return out


class FakeRedisWithPipeline:
    """FakeRedis whose only supported access pattern is pipelining --
    used to assert ``load_many`` actually batches instead of falling
    back to one call per symbol."""

    def __init__(self):
        self.lists: dict[str, list] = {}
        self.lrange_call_count = 0

    def lrange(self, key, start, end):
        # Present so load() (single-symbol) still works, but load_many
        # must not call this per-symbol -- see the assertion below.
        self.lrange_call_count += 1
        values = self.lists.get(key, [])
        end_idx = len(values) - 1 if end == -1 else end
        return values[start : end_idx + 1]

    def pipeline(self, transaction=False):
        return FakePipeline(self.lists)


class OnceThenHangStrategy(Strategy):
    """First scan() hangs forever (simulating an unbounded funding read
    inside asyncio.to_thread); a later scan() would set ``recovered``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scan_calls = 0
        self.recovered = False

    def get_required_channels_instance(self) -> list[str]:
        return ["kline:binance:1d"]

    def get_warmup_symbols(self) -> list[str]:
        return ["BTCUSDT"]

    def get_warmup_tfs(self) -> list[str]:
        return ["1d"]

    def get_warmup_bars(self, tf: str) -> int:
        return 2

    async def scan(self) -> None:
        self.scan_calls += 1
        if self.scan_calls == 1:
            # Stand-in for a funding read stuck on a dead connection with
            # no timeout: never returns on its own.
            await asyncio.sleep(999)
        else:
            self.recovered = True


@pytest.mark.asyncio
async def test_event_loop_recovers_and_processes_next_candle_after_scan_hangs(
    monkeypatch,
):
    """A strategy whose scan() hangs on one event must not silence the
    alpha forever: the loop must eventually time out that event and go
    on to process the strategy's *next* event (e.g. the following day's
    candle close), instead of waiting on the hung call indefinitely.

    Before U4 (watchdog around handle_strategy_event), this hangs for
    the full asyncio.sleep(999) and the assertion below times out --
    reproducing exactly how the 5 daily alphas never recovered on their
    own and required a manual runner restart. The real per-timeframe
    thresholds are tens of seconds (see .agents/PLAN.md), so the default
    fallback is shortened here purely so the test itself stays fast --
    the watchdog mechanism being tested is the same either way.
    """
    monkeypatch.setattr(runner_main, "_DEFAULT_EVENT_TIMEOUT_SEC", 0.2)

    ctx = StrategyContext(
        "1d-test", "1", SharedCandleCache(), None, StrategyRuntimeState(ready=True)
    )
    strategy = OnceThenHangStrategy(alpha_id="1d-test", version="1", params={}, ctx=ctx)

    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(
        runner_main.run_strategy_event_loop(strategy, queue, stop)
    )

    try:
        # Event 1: universe refresh / candle close that hangs inside scan().
        await queue.put(
            DataEvent("symbols:binance", "symbols", "", "", {"symbols": []})
        )
        await asyncio.sleep(0.05)  # let the loop pick it up and start hanging
        assert strategy.scan_calls == 1

        # Event 2: the *next* candle close for the same alpha (e.g. next
        # day's 00:00 UTC bar). The loop must not still be blocked on
        # event 1 -- it must have moved on within a bounded time.
        await queue.put(DataEvent("kline:binance:1d", "kline", "BTCUSDT", "1d", {}))

        deadline = asyncio.get_event_loop().time() + 2.0
        while not strategy.recovered and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert strategy.recovered, (
            "alpha never processed its next event after scan() hung -- "
            "matches the 2026-07-16 incident where 5 daily alphas went "
            "silent forever until the runner was restarted"
        )
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_funding_snapshot_load_many_batches_into_a_single_round_trip():
    """Before U2, `_attach_funding_panel` called `reader.load(symbol)` once
    per symbol -- ~180 sequential blocking round-trips per scan for a
    full cross-sectional universe, each one a chance to stall. Batched
    reads must cost exactly one pipeline execute, not one lrange call
    per symbol.
    """
    redis = FakeRedisWithPipeline()
    symbols = [f"SYM{i}USDT" for i in range(180)]
    for symbol in symbols:
        redis.lists[f"funding_snapshot:binance:{symbol}"] = [
            json.dumps(
                {"symbol": symbol, "funding_time": 1_000, "funding_rate": 0.0001}
            ),
        ]
    reader = FundingSnapshotReader(redis, "binance")

    result = reader.load_many(symbols)

    assert redis.lrange_call_count == 0, (
        "load_many must batch via pipeline, not fall back to one lrange call per symbol"
    )
    assert len(result) == 180
    assert result["SYM0USDT"][0]["funding_rate"] == 0.0001


def test_mds_redis_client_is_constructed_with_a_bounded_socket_timeout(
    monkeypatch, tmp_path
):
    """The 2026-07-16 incident's root cause was an mds-redis connection
    with no socket timeout: a stalled read blocked its thread forever,
    permanently shrinking the runner's shared compute pool by one every
    time it happened. `mds_client` is only ever used for bounded
    request/response calls (warmup, snapshot, funding) -- never the
    long-lived pubsub subscription, which SharedPubSubManager opens on
    its own separate connection -- so it is safe, and required, for it
    to have a socket timeout.
    """
    captured: dict = {}

    class FakeSyncRedis:
        _runner_inline_redis = True

        def pubsub(self):
            return object()

    def fake_from_url(url, **kwargs):
        captured.setdefault("calls", []).append((url, kwargs))
        return FakeSyncRedis()

    monkeypatch.setattr(runner_main.redis, "from_url", fake_from_url)

    path = tmp_path / "runner.yaml"
    path.write_text(
        "runner_id: r\nshadow_mode: true\nredis_url: redis://paper\n"
        "mds_redis_url: redis://mds\nalphas: []\n",
        encoding="utf-8",
    )

    async def _drive():
        # dry_run bypasses this client construction entirely; force the
        # non-dry-run branch just far enough to construct mds_client, then
        # let it fail naturally afterwards (no real redis available here).
        # Bounded by wait_for as a defensive backstop in case FakeSyncRedis
        # doesn't fail as fast as expected.
        await asyncio.wait_for(runner_main.run(str(path), dry_run=False), timeout=2.0)

    with contextlib.suppress(Exception):
        asyncio.run(_drive())

    mds_calls = [c for c in captured.get("calls", []) if c[0] == "redis://mds"]
    assert mds_calls, "mds_client was never constructed via redis.from_url"
    _, kwargs = mds_calls[0]
    assert kwargs.get("socket_timeout"), "mds_client must have a bounded socket_timeout"
    assert kwargs.get("socket_connect_timeout"), (
        "mds_client must have a bounded socket_connect_timeout"
    )


@pytest.mark.asyncio
async def test_loop_abandons_event_when_admission_semaphore_is_saturated(monkeypatch):
    """A strategy whose queue sits behind a fully-held admission semaphore
    (a universe refresh fans out to every alpha's queue at once and can
    park every permit) must abandon the event after the same deadline as
    the handler, instead of parking its loop forever on the semaphore
    acquire.

    Before the 2026-08-30 fix, the `async with scan_semaphore` had no
    timeout: the loop waited indefinitely, silently stopped draining its
    queue, and dropped every later event -- including the next daily
    close (the 1d-iamp queue never drained from 15:00Z and its 00:00:37Z
    close was dropped, so the 30/08 basket was never computed).
    """
    monkeypatch.setattr(runner_main, "_DEFAULT_EVENT_TIMEOUT_SEC", 0.2)

    ctx = StrategyContext(
        "1d-test", "1", SharedCandleCache(), None, StrategyRuntimeState(ready=True)
    )
    strategy = OnceThenHangStrategy(alpha_id="1d-test", version="1", params={}, ctx=ctx)

    sem = asyncio.Semaphore(1)
    await sem.acquire()  # exhaust the only permit; nobody will release it

    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(
        runner_main.run_strategy_event_loop(strategy, queue, stop, scan_semaphore=sem)
    )

    try:
        # Non-critical event behind a saturated semaphore: the loop must
        # drop it after the bounded admission wait and go back to waiting
        # for queue items, NOT stay parked on the acquire forever.
        await queue.put(
            DataEvent("symbols:binance", "symbols", "", "", {"symbols": []})
        )
        await asyncio.sleep(0.5)
        assert strategy.scan_calls == 0, (
            "event must be abandoned after the bounded admission wait, "
            "not processed while the semaphore is exhausted"
        )

        # The loop must still be alive: once a permit is released, the
        # next event is processed -- and the abandoned event must NOT be
        # processed afterwards. Give the old code room to show its bug:
        # it processes the abandoned event (1) AND then the new one (2);
        # the fixed code stays at 1.
        sem.release()
        await queue.put(
            DataEvent("symbols:binance", "symbols", "", "", {"symbols": []})
        )
        deadline = asyncio.get_event_loop().time() + 2.0
        while strategy.scan_calls < 1 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.4)
        assert strategy.scan_calls == 1, (
            "the loop must process the post-release event and must not "
            "double-process the abandoned one (got %d scan calls)" % strategy.scan_calls
        )
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_loop_processes_critical_kline_even_when_semaphore_is_saturated(
    monkeypatch,
):
    """A kline on the strategy's own timeframe is its rebalance trigger
    (the midnight-UTC close for 1d alphas). When the admission semaphore
    is saturated past the deadline, the event must still be processed
    without admission control -- dropping it would silently skip that
    day's rebalance (2026-08-30 incident).
    """
    monkeypatch.setattr(runner_main, "_DEFAULT_EVENT_TIMEOUT_SEC", 0.2)
    # kline events on tf=1d would otherwise get the real 300s timeout
    # (_EVENT_TIMEOUT_SEC["1d"]), making the test wait forever.
    monkeypatch.setattr(runner_main, "_EVENT_TIMEOUT_SEC", {"1d": 0.2})

    ctx = StrategyContext(
        "1d-test", "1", SharedCandleCache(), None, StrategyRuntimeState(ready=True)
    )
    strategy = OnceThenHangStrategy(alpha_id="1d-test", version="1", params={}, ctx=ctx)
    strategy.spec = SimpleNamespace(timeframe="1d")

    sem = asyncio.Semaphore(1)
    await sem.acquire()  # exhaust the only permit; nobody will release it

    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(
        runner_main.run_strategy_event_loop(strategy, queue, stop, scan_semaphore=sem)
    )

    try:
        await queue.put(DataEvent("kline:binance:1d", "kline", "BTCUSDT", "1d", {}))
        deadline = asyncio.get_event_loop().time() + 1.0
        while strategy.scan_calls < 1 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert strategy.scan_calls == 1, (
            "own-timeframe kline must be processed even while the "
            "admission semaphore is exhausted (rebalance trigger)"
        )
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
