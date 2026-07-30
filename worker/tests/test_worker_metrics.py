import pytest

from app.db import Database
from app.executor import Executor
from app.main import process_signal_message
from app.metrics import WorkerMetrics


@pytest.fixture
async def setup(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    executor = Executor(database, slippage_pct=0.0)
    yield database, executor
    await database.close()


def _open(signal_id: str, alpha_id: str = "test-alpha") -> dict:
    return {
        "type": "OPEN",
        "alpha_id": alpha_id,
        "signal_id": signal_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "95000.0",
        "qty": "0.01",
        "timestamp": "2026-05-22T10:00:00Z",
    }


# ---- WorkerMetrics unit ----

def test_metrics_invariant_holds_after_mixed_counts():
    m = WorkerMetrics()
    m.inc("received_total", 4)
    m.inc("duplicate_skipped_total")
    m.inc_parse_error("a")
    m.inc_process_error("b")
    m.inc_committed("OPEN")
    assert m.committed_total == 1
    assert m.reconciles() is True
    snap = m.snapshot()
    assert snap["reconciles"] is True
    assert snap["parse_error_by_alpha"] == {"a": 1}
    assert snap["process_error_by_alpha"] == {"b": 1}


def test_metrics_invariant_breaks_when_unaccounted():
    m = WorkerMetrics()
    m.inc("received_total", 2)
    m.inc_committed("OPEN")  # only 1 accounted of 2 received
    assert m.reconciles() is False
    assert m.snapshot()["reconciles"] is False


# ---- through process_signal_message ----

@pytest.mark.asyncio
async def test_open_increments_received_and_committed(setup):
    db, executor = setup
    m = WorkerMetrics()
    await process_signal_message(_open("sig-1"), db, executor, metrics=m)
    assert m.received_total == 1
    assert m.committed_by_type == {"OPEN": 1}
    assert m.reconciles()


@pytest.mark.asyncio
async def test_duplicate_increments_duplicate_skipped(setup):
    db, executor = setup
    m = WorkerMetrics()
    await process_signal_message(_open("dup-1"), db, executor, metrics=m)
    await process_signal_message(_open("dup-1"), db, executor, metrics=m)
    assert m.received_total == 2
    assert m.duplicate_skipped_total == 1
    assert m.committed_total == 1
    assert m.reconciles()


@pytest.mark.asyncio
async def test_parse_error_increments_parse_error_by_alpha(setup):
    db, executor = setup
    m = WorkerMetrics()
    bad = {"type": "GARBAGE", "alpha_id": "bad-alpha", "signal_id": "sig-bad"}
    result = await process_signal_message(bad, db, executor, metrics=m)
    assert result is None
    assert m.received_total == 1
    assert m.parse_error_total == 1
    assert m.parse_error_by_alpha == {"bad-alpha": 1}
    assert m.reconciles()


@pytest.mark.asyncio
async def test_process_error_increments_process_error_by_alpha(setup, monkeypatch):
    db, executor = setup
    m = WorkerMetrics()

    async def boom(*args, **kwargs):
        raise RuntimeError("executor exploded")

    monkeypatch.setattr(executor, "process_open", boom)
    result = await process_signal_message(_open("sig-err", alpha_id="err-alpha"),
                                          db, executor, metrics=m)
    assert result is None
    assert m.received_total == 1
    assert m.process_error_total == 1
    assert m.process_error_by_alpha == {"err-alpha": 1}
    assert m.committed_total == 0
    assert m.reconciles()


@pytest.mark.asyncio
async def test_metrics_none_does_not_break(setup):
    db, executor = setup
    result = await process_signal_message(_open("sig-none"), db, executor)
    assert result["position_id"] is not None
