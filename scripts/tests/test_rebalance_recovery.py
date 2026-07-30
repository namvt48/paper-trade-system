"""Regression tests for the staged rebalance recovery workflow."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts.rebalance_recovery.candidate import build_main_candidate
from scripts.rebalance_recovery.domain import (
    AlphaId,
    CandleOpenMs,
    CloseLedgerEntry,
    OpenLedgerEntry,
    PositionId,
    Side,
    SignalId,
)
from scripts.rebalance_recovery.promotion import (
    ApprovalMismatchError,
    PromotionError,
    promote_validated_workspace,
    verify_approval,
)
from scripts.rebalance_recovery.replay_state import load_position_state
from scripts.rebalance_recovery.scope import (
    EXCLUDED_ALPHA,
    build_close_points,
    build_incident_points,
)


def _create_source_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE alphas (
                alpha_id TEXT PRIMARY KEY,
                display_name TEXT,
                created_at TEXT,
                status TEXT DEFAULT 'active'
            );
            CREATE TABLE positions (
                position_id TEXT PRIMARY KEY,
                alpha_id TEXT,
                signal_id TEXT,
                symbol TEXT,
                side TEXT,
                entry_price REAL,
                qty REAL,
                tp REAL,
                sl REAL,
                leverage INTEGER,
                opened_at TEXT,
                metadata TEXT,
                exchange TEXT,
                fee_pct REAL,
                mode TEXT DEFAULT 'paper',
                status TEXT DEFAULT 'OPEN'
            );
            CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY,
                position_id TEXT,
                alpha_id TEXT,
                signal_id TEXT,
                symbol TEXT,
                side TEXT,
                entry_price REAL,
                exit_price REAL,
                qty REAL,
                pnl REAL,
                pnl_percent REAL,
                leverage INTEGER,
                tp REAL,
                sl REAL,
                reason TEXT,
                duration_hours REAL,
                opened_at TEXT,
                closed_at TEXT,
                metadata TEXT,
                fee REAL,
                exchange TEXT,
                mode TEXT DEFAULT 'paper'
            );
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT,
                alpha_id TEXT,
                type TEXT,
                payload TEXT,
                received_at TEXT,
                processed INTEGER,
                error TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO alphas VALUES (?, ?, ?, ?)",
            (
                "15m-blend-close",
                "15m-blend-close",
                "2026-06-24T00:00:00+00:00",
                "active",
            ),
        )
        conn.execute(
            """INSERT INTO positions
               (position_id, alpha_id, signal_id, symbol, side, entry_price, qty,
                leverage, opened_at, metadata, exchange, fee_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "old-position",
                "15m-blend-close",
                "old-open",
                "BTCUSDT",
                "LONG",
                100.0,
                2.0,
                1,
                "2026-07-15T00:00:05+00:00",
                "{}",
                "binance",
                0.0007,
            ),
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_incident_scope_has_exactly_thirty_missing_rebalances() -> None:
    points = build_incident_points(date(2026, 7, 16), date(2026, 7, 17))

    assert len(points) == 30
    assert all(point.alpha_id != EXCLUDED_ALPHA for point in points)
    assert {str(point.alpha_id) for point in points if "36h" in point.alpha_id} == set()
    assert sum(point.alpha_id == AlphaId("1d-chmom") for point in points) == 1
    assert sum(point.alpha_id == AlphaId("ensemble-1d") for point in points) == 1


def test_only_alphas_restricts_scope_to_the_allowlist() -> None:
    five_daily = [
        "1d-kertrend",
        "1d-vwaprev",
        "1d-iamp",
        "1d-chmom",
        "ensemble-1d",
    ]
    points = build_incident_points(
        date(2026, 7, 18), date(2026, 7, 18), only_alphas=five_daily
    )

    # Exactly one cycle per daily alpha at the single 18/07 midnight close, and
    # nothing else (healthy intraday alphas due at midnight are excluded).
    assert {str(point.alpha_id) for point in points} == set(five_daily)
    assert len(points) == len(five_daily)
    assert all(point.timeframe == "1d" for point in points)


def test_only_alphas_none_preserves_full_frozen_schedule() -> None:
    baseline = build_incident_points(date(2026, 7, 16), date(2026, 7, 17))
    same = build_incident_points(
        date(2026, 7, 16), date(2026, 7, 17), only_alphas=None
    )

    assert same == baseline
    assert len(same) == 30


def test_only_alphas_rejects_unknown_alpha_id() -> None:
    with pytest.raises(ValueError, match="not in incident schedule"):
        build_incident_points(
            date(2026, 7, 18), date(2026, 7, 18), only_alphas=["does-not-exist"]
        )


def test_explicit_close_builds_36h_points_at_the_same_wall_clock() -> None:
    affected = [
        "15m-blend-close-36h",
        "15m-blend-close-b-36h",
        "1h-decay-close-36h",
        "1h-decay-vwap-36h",
        "15m-trend-close-36h-reverse",
        "15m-trend-vwap-36h-reverse",
    ]
    close_at = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

    points = build_close_points(close_at, affected)

    assert {str(point.alpha_id) for point in points} == set(affected)
    assert {point.event_at for point in points} == {
        datetime(2026, 7, 25, 12, 0, 5, tzinfo=timezone.utc)
    }
    for point in points:
        timeframe_ms = 900_000 if point.timeframe == "15m" else 3_600_000
        assert int(point.candle_open_ms) == int(close_at.timestamp() * 1_000) - timeframe_ms


def test_candidate_build_never_mutates_source_database(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    candidate = tmp_path / "candidate.db"
    _create_source_db(source)
    source_before = _sha256(source)
    event_at = datetime(2026, 7, 16, 0, 0, 5, tzinfo=timezone.utc)
    entries = (
        CloseLedgerEntry(
            alpha_id=AlphaId("15m-blend-close"),
            signal_id=SignalId("close-signal"),
            position_id=PositionId("old-position"),
            symbol="BTCUSDT",
            side=Side.LONG,
            decision_price=110.0,
            candle_open_ms=CandleOpenMs(1_784_156_700_000),
            event_at=event_at,
            fee_pct=0.0007,
        ),
        OpenLedgerEntry(
            alpha_id=AlphaId("15m-blend-close"),
            signal_id=SignalId("open-signal"),
            position_id=PositionId("new-position"),
            symbol="ETHUSDT",
            side=Side.SHORT,
            decision_price=50.0,
            qty=4.0,
            weight=-0.5,
            strategy_leverage=1.0,
            candle_open_ms=CandleOpenMs(1_784_156_700_000),
            event_at=event_at,
            fee_pct=0.0007,
        ),
    )

    build_main_candidate(source, candidate, entries, recovered_at=event_at)

    assert _sha256(source) == source_before
    with sqlite3.connect(candidate) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        assert conn.execute("SELECT symbol FROM positions").fetchone()[0] == "ETHUSDT"
        assert (
            conn.execute("SELECT COUNT(*) FROM signals WHERE processed = 1").fetchone()[
                0
            ]
            == 2
        )


def test_promotion_rejects_wrong_approval_hash() -> None:
    with pytest.raises(ApprovalMismatchError):
        verify_approval(expected_hash="abc", supplied_hash="def")

    verify_approval(expected_hash="abc", supplied_hash="abc")


def test_replay_state_retains_entry_price_for_missing_close_quote(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    _create_source_db(source)

    state = load_position_state(source)

    assert state[AlphaId("15m-blend-close")]["BTCUSDT"].entry_price == 100.0


def test_promotion_requires_explicit_writer_shutdown(tmp_path: Path) -> None:
    with pytest.raises(PromotionError, match="services-stopped"):
        promote_validated_workspace(
            workspace=tmp_path,
            production_main=tmp_path / "main.db",
            production_equity=tmp_path / "equity.db",
            redis_url="redis://unused",
            approval_hash="unused",
            services_stopped=False,
        )
