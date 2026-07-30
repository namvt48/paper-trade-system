"""Regression tests for the 2026-07-27 incident: a burst of ~40 simultaneous
alpha rebalances at 00:00 UTC pushed SQLite past its busy_timeout, raising
"database is locked" from ``db.transaction()``'s ``BEGIN IMMEDIATE`` -- a
call site outside ``process_signal_message``'s internal try/except. That
exception propagated out of the consumer's per-message handling and crashed
the whole worker process. Every message already delivered by that
XREADGROUP batch (including 1h-blend-close's entire close+reopen cycle)
was left un-acked in the stream's PEL forever, since CONSUMER_NAME is fixed
and the loop only reads new ">" entries after a restart.

These tests reproduce the mechanism at unit-test speed and must fail on the
pre-fix code (RED) -- an unhandled exception from ``process_signal_message``
propagating out of message handling and skipping ack -- before the
``handle_signal_message`` try/except (GREEN) lands.
"""

from __future__ import annotations

import contextlib

import pytest

from app import main as worker_main
from app.db import Database
from app.executor import Executor
from app.metrics import WorkerMetrics


class FakePaperRedis:
    """Minimal stand-in for the paper-redis client: records xack calls."""

    def __init__(self) -> None:
        self.acked: list[tuple[str, str, str]] = []

    async def xack(self, stream: str, group: str, msg_id: str) -> None:
        self.acked.append((stream, group, msg_id))


def _open(
    signal_id: str, alpha_id: str = "1h-blend-close", symbol: str = "BTCUSDT"
) -> dict:
    return {
        "type": "OPEN",
        "alpha_id": alpha_id,
        "signal_id": signal_id,
        "symbol": symbol,
        "side": "LONG",
        "entry": "95000.0",
        "qty": "0.01",
        "timestamp": "2026-07-27T00:00:00Z",
    }


@pytest.fixture
async def setup(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    executor = Executor(database, slippage_pct=0.0)
    yield database, executor
    await database.close()


async def _handle(db, executor, data, paper_redis, worker_metrics, msg_id="1-0"):
    await worker_main.handle_signal_message(
        msg_id,
        data,
        db=db,
        executor=executor,
        fill_service=None,
        snapshot_publisher=None,
        orderbook_enabled=False,
        supported_exchanges=set(),
        mds_redis=None,
        ob_cache=None,
        paper_redis=paper_redis,
        worker_metrics=worker_metrics,
    )


@pytest.mark.asyncio
async def test_db_lock_error_does_not_crash_consumer_and_leaves_message_pending(
    setup, monkeypatch
):
    db, executor = setup
    paper_redis = FakePaperRedis()
    metrics = WorkerMetrics()

    # Faithfully reproduce the incident: BEGIN IMMEDIATE fails when *entering*
    # db.transaction(), before process_signal_message's own try/except (which
    # only wraps the body of the `async with`) ever gets a chance to run.
    @contextlib.asynccontextmanager
    async def locked_transaction():
        raise Exception("database is locked")
        yield  # pragma: no cover -- unreachable, required to stay a generator

    monkeypatch.setattr(db, "transaction", locked_transaction)

    # Must not raise -- a single message's DB contention can no longer take
    # down the whole consumer loop.
    await _handle(db, executor, _open("sig-locked"), paper_redis, metrics)

    assert paper_redis.acked == []  # left un-acked -- redeliverable via XCLAIM
    assert metrics.received_total == 1
    assert metrics.left_pending_total == 1
    assert metrics.left_pending_by_alpha == {"1h-blend-close": 1}
    assert metrics.reconciles()


@pytest.mark.asyncio
async def test_one_crashed_message_does_not_stop_later_messages_in_the_batch(
    setup, monkeypatch
):
    """The historical bug: one crash mid-batch orphaned every message that
    hadn't been reached yet in the same XREADGROUP batch (they were already
    "delivered", so a restart's fresh XREADGROUP ">" call never redelivers
    them). Simulating a batch of 3 where the middle one fails must not
    prevent the third from being processed and acked."""
    db, executor = setup
    paper_redis = FakePaperRedis()
    metrics = WorkerMetrics()

    real_transaction = db.transaction
    calls = []

    @contextlib.asynccontextmanager
    async def maybe_locked():
        if calls and calls[-1] == "sig-2":
            raise Exception("database is locked")
            yield  # pragma: no cover -- unreachable
        else:
            async with real_transaction():
                yield

    real_process = worker_main.process_signal_message

    async def tracking_process(data, *args, **kwargs):
        calls.append(data["signal_id"])
        return await real_process(data, *args, **kwargs)

    monkeypatch.setattr(db, "transaction", maybe_locked)
    monkeypatch.setattr(worker_main, "process_signal_message", tracking_process)

    batch = [
        _open("sig-1", symbol="AAAUSDT"),
        _open("sig-2", symbol="BBBUSDT"),
        _open("sig-3", symbol="CCCUSDT"),
    ]
    for i, data in enumerate(batch):
        await _handle(db, executor, data, paper_redis, metrics, msg_id=f"{i}-0")

    assert calls == ["sig-1", "sig-2", "sig-3"]
    # sig-1 and sig-3 committed and acked normally; only sig-2 stayed pending.
    acked_msg_ids = [msg_id for _, _, msg_id in paper_redis.acked]
    assert acked_msg_ids == ["0-0", "2-0"]
    assert metrics.left_pending_total == 1
    assert metrics.committed_total == 2


@pytest.mark.asyncio
async def test_happy_path_still_acks_normally(setup):
    """Regression guard: the extraction into handle_signal_message must not
    change the ordinary success path (ack + xack_total)."""
    db, executor = setup
    paper_redis = FakePaperRedis()
    metrics = WorkerMetrics()

    await _handle(db, executor, _open("sig-ok"), paper_redis, metrics, msg_id="5-0")

    assert len(paper_redis.acked) == 1
    assert metrics.xack_total == 1
    assert metrics.left_pending_total == 0
    assert metrics.reconciles()
