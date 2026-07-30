"""Regression tests for runner strategies adopting the worker's authoritative
open-position snapshot on startup.

Incident (2026-07-02, alpha ``deobietcophaialphakhong``): a runner strategy restored
``_open_positions`` purely from its own ``runner:positions:{alpha_id}`` cache and never
reconciled against the worker DB snapshot (``paper:positions:snapshot:{alpha_id}``). The
two silently diverged -- the DB held a position the engine no longer managed (never
closed), while every new OPEN on that symbol was rejected by
``DUPLICATE_POSITION_POLICY=reject``. The fleet sweep found this across 7 legacy alphas
(alpha2-NR-* had 385/260 phantom positions). Both ``legacy_standalone`` and
``cross_sectional`` share ``Strategy.reconcile_open_positions`` so neither can diverge.
"""

from __future__ import annotations

import json

from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState
from runner.strategy.base import Strategy, merge_authoritative_positions
from runner.strategy.context import StrategyContext

from base.position_reconcile import normalize_position


ALPHA = "deobietcophaialphakhong"


class FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def delete(self, key):
        self.values.pop(key, None)
        return 1


class _ReconcileStrategy(Strategy):
    """Minimal concrete Strategy to exercise the inherited reconcile method."""

    def get_required_channels_instance(self):
        return []

    def get_warmup_symbols(self):
        return []

    def get_warmup_tfs(self):
        return []

    def get_warmup_bars(self, tf):
        return 0


def _worker_snapshot(positions):
    return json.dumps(
        {
            "alpha_id": ALPHA,
            "generated_at": "2026-07-22T09:26:20+00:00",
            "positions": positions,
            "revision": 1,
        }
    )


def _db_long():
    return {
        "position_id": "287de148",
        "alpha_id": ALPHA,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": 61749.65,
        "qty": 0.081,
        "tp": 63297.34,
        "sl": 60672.81,
        "leverage": 50,
        "exchange": "binance",
        "fee_pct": 0.000357,
        "opened_at": "2026-07-02T17:45:00+00:00",
        "metadata": {"milestone": 0, "is_reverse": True, "trade_size": 5000.0},
        "status": "OPEN",
    }


def _runner_short_phantom():
    return {
        "BTCUSDT": {
            "position_id": "0a19b8db",
            "side": "SHORT",
            "entry": 66624.95,
            "sl": 66558.33,
            "tp": 65292.46,
            "size": 5000.0,
            "milestone": 1,
            "is_reverse": False,
            "entry_candle_open_ms": 1784642400000,
            "signal_candle_close_ms": 1784643300000,
        }
    }


def _ctx(redis_client):
    return StrategyContext(
        ALPHA,
        "1",
        SharedCandleCache(),
        None,
        StrategyRuntimeState(),
        redis_client=redis_client,
    )


def _strategy(fake_redis):
    return _ReconcileStrategy(ALPHA, "1", {}, _ctx(fake_redis))


def _snap_key():
    return f"paper:positions:snapshot:{ALPHA}"


def _runner_key():
    return f"runner:positions:{ALPHA}"


# ── ctx.load_authoritative_positions ──────────────────────────────────────────


def test_load_authoritative_positions_reads_worker_snapshot():
    fake = FakeRedis({_snap_key(): _worker_snapshot([_db_long()])})
    result = _ctx(fake).load_authoritative_positions()
    assert set(result) == {"287de148"}
    assert result["287de148"]["symbol"] == "BTCUSDT"
    assert result["287de148"]["side"] == "LONG"
    assert result["287de148"]["entry"] == 61749.65


def test_load_authoritative_positions_none_when_no_snapshot():
    assert _ctx(FakeRedis()).load_authoritative_positions() is None


def test_load_authoritative_positions_empty_dict_when_snapshot_empty():
    fake = FakeRedis({_snap_key(): _worker_snapshot([])})
    assert _ctx(fake).load_authoritative_positions() == {}


# ── merge_authoritative_positions (pure) ──────────────────────────────────────


def test_merge_adopts_snapshot_position_when_runner_cache_forgot_it():
    authoritative = {"287de148": normalize_position(_db_long())}
    reconciled, adopted, dropped = merge_authoritative_positions({}, authoritative)
    assert set(reconciled) == {"BTCUSDT"}
    assert reconciled["BTCUSDT"]["position_id"] == "287de148"
    assert adopted == ["287de148"]
    assert dropped == []


def test_merge_prefers_runner_runtime_on_id_match():
    runner_store = {
        "BTCUSDT": {
            "position_id": "287de148",
            "side": "LONG",
            "entry": 61749.65,
            "sl": 60672.81,
            "tp": 63297.34,
            "size": 5000.0,
            "milestone": 2,
            "is_reverse": True,
        }
    }
    authoritative = {"287de148": normalize_position(_db_long())}
    reconciled, adopted, dropped = merge_authoritative_positions(
        runner_store, authoritative
    )
    assert reconciled["BTCUSDT"]["milestone"] == 2
    assert adopted == []
    assert dropped == []


def test_merge_drops_runner_phantom_absent_from_snapshot():
    reconciled, adopted, dropped = merge_authoritative_positions(
        _runner_short_phantom(), {}
    )
    assert reconciled == {}
    assert dropped == ["0a19b8db"]
    assert adopted == []


def test_merge_reproduces_split_brain_incident():
    authoritative = {"287de148": normalize_position(_db_long())}
    reconciled, adopted, dropped = merge_authoritative_positions(
        _runner_short_phantom(), authoritative
    )
    assert set(reconciled) == {"BTCUSDT"}
    assert reconciled["BTCUSDT"]["position_id"] == "287de148"
    assert reconciled["BTCUSDT"]["side"] == "LONG"
    assert adopted == ["287de148"]
    assert dropped == ["0a19b8db"]


# ── Strategy.reconcile_open_positions (shared by both strategy types) ──────────


def test_reconcile_adopts_db_position_runner_forgot():
    fake = FakeRedis({_snap_key(): _worker_snapshot([_db_long()])})
    result = _strategy(fake).reconcile_open_positions()
    assert set(result) == {"BTCUSDT"}
    assert result["BTCUSDT"]["position_id"] == "287de148"


def test_reconcile_drops_phantom_when_db_empty():
    fake = FakeRedis(
        {
            _snap_key(): _worker_snapshot([]),
            _runner_key(): json.dumps(_runner_short_phantom()),
        }
    )
    assert _strategy(fake).reconcile_open_positions() == {}


def test_reconcile_split_brain_end_to_end():
    fake = FakeRedis(
        {
            _snap_key(): _worker_snapshot([_db_long()]),
            _runner_key(): json.dumps(_runner_short_phantom()),
        }
    )
    result = _strategy(fake).reconcile_open_positions()
    assert set(result) == {"BTCUSDT"}
    assert result["BTCUSDT"]["position_id"] == "287de148"
    assert result["BTCUSDT"]["side"] == "LONG"


def test_reconcile_falls_back_to_runner_cache_when_snapshot_absent():
    # Worker snapshot missing must NOT wipe live positions.
    fake = FakeRedis({_runner_key(): json.dumps(_runner_short_phantom())})
    result = _strategy(fake).reconcile_open_positions()
    assert set(result) == {"BTCUSDT"}
    assert result["BTCUSDT"]["position_id"] == "0a19b8db"
