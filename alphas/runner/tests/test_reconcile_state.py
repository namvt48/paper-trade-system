from __future__ import annotations

from runner.reconcile.state import StrategyRuntimeState


def test_reconcile_state_no_positions_is_valid():
    state = StrategyRuntimeState(ready=True)
    state.mark_stale_snapshot(has_positions=False)
    assert state.reconcile_stale is False
    assert state.can_open_new_trades() is True


def test_reconcile_state_stale_snapshot_with_positions_suspends_entries_only():
    state = StrategyRuntimeState(ready=True)
    state.mark_stale_snapshot(has_positions=True)
    assert state.can_open_new_trades() is False
    assert state.can_manage_existing_positions() is True


def test_reconcile_state_redis_down_only_affects_one_strategy():
    a = StrategyRuntimeState(ready=True)
    b = StrategyRuntimeState(ready=True)
    a.mark_redis_error()

    assert a.can_open_new_trades() is False
    assert b.can_open_new_trades() is True


def test_reconcile_clearing_does_not_clear_data_stale():
    state = StrategyRuntimeState(ready=True, data_stale=True, reconcile_stale=True)
    state.mark_reconcile_ok()

    assert state.reconcile_stale is False
    assert state.data_stale is True
    assert state.can_open_new_trades() is False

