from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from scripts.rebalance_recovery.merge import (
    DatabaseMergeError,
    merge_equity_database,
    merge_main_database,
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_main(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE positions (
                position_id TEXT PRIMARY KEY, alpha_id TEXT, symbol TEXT
            );
            CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY, alpha_id TEXT, pnl REAL
            );
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, alpha_id TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO positions VALUES (?, ?, ?)",
            (
                ("affected-old", "alpha-a", "BTCUSDT"),
                ("untouched", "alpha-b", "ETHUSDT"),
            ),
        )
        conn.execute("INSERT INTO trades VALUES ('old-trade', 'alpha-b', 1.0)")
        conn.execute(
            "INSERT INTO signals (signal_id, alpha_id) VALUES ('old-signal', 'alpha-b')"
        )


def _create_equity(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE equity_snapshots (
                id INTEGER PRIMARY KEY, timestamp TEXT, alpha_id TEXT,
                balance REAL, unrealized_pnl REAL, realized_pnl REAL
            )"""
        )
        conn.executemany(
            "INSERT INTO equity_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (
                (1, "2026-07-15T00:00:00+00:00", "alpha-a", 10.0, 0.0, 0.0),
                (2, "2026-07-16T00:05:00+00:00", "alpha-a", 10.0, 0.0, 0.0),
                (3, "2026-07-16T00:05:00+00:00", "__TOTAL__", 20.0, 0.0, 0.0),
            ),
        )


def test_main_merge_appends_history_and_reconciles_only_affected_book(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.db"
    candidate = tmp_path / "candidate.db"
    production = tmp_path / "production.db"
    _create_main(baseline)
    shutil.copy2(baseline, candidate)
    shutil.copy2(baseline, production)
    with sqlite3.connect(candidate) as conn:
        conn.execute("DELETE FROM positions WHERE alpha_id = 'alpha-a'")
        conn.execute(
            "INSERT INTO positions VALUES ('affected-new', 'alpha-a', 'SOLUSDT')"
        )
        conn.execute("INSERT INTO trades VALUES ('recovered-trade', 'alpha-a', 2.0)")
        conn.execute(
            "INSERT INTO signals (signal_id, alpha_id) VALUES ('recovered-signal', 'alpha-a')"
        )
    baseline_hash = _hash(baseline)

    merge_main_database(production, baseline, candidate, ("alpha-a",))

    assert _hash(baseline) == baseline_hash
    with sqlite3.connect(production) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM trades WHERE trade_id = 'old-trade'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM trades WHERE trade_id = 'recovered-trade'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT position_id FROM positions WHERE alpha_id = 'alpha-b'"
            ).fetchone()[0]
            == "untouched"
        )
        assert (
            conn.execute(
                "SELECT position_id FROM positions WHERE alpha_id = 'alpha-a'"
            ).fetchone()[0]
            == "affected-new"
        )


def test_equity_merge_updates_values_without_adding_or_deleting_rows(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline-equity.db"
    candidate = tmp_path / "candidate-equity.db"
    production = tmp_path / "production-equity.db"
    _create_equity(baseline)
    shutil.copy2(baseline, candidate)
    shutil.copy2(baseline, production)
    with sqlite3.connect(candidate) as conn:
        conn.execute(
            "UPDATE equity_snapshots SET balance = 12.0, realized_pnl = 2.0 WHERE id IN (2, 3)"
        )

    merge_equity_database(production, baseline, candidate)

    with sqlite3.connect(production) as conn:
        assert conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 3
        assert (
            conn.execute(
                "SELECT balance FROM equity_snapshots WHERE id = 1"
            ).fetchone()[0]
            == 10.0
        )
        assert (
            conn.execute(
                "SELECT balance FROM equity_snapshots WHERE id = 2"
            ).fetchone()[0]
            == 12.0
        )


def test_main_merge_rejects_production_drift_without_partial_changes(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.db"
    candidate = tmp_path / "candidate.db"
    production = tmp_path / "production.db"
    _create_main(baseline)
    shutil.copy2(baseline, candidate)
    shutil.copy2(baseline, production)
    with sqlite3.connect(candidate) as conn:
        conn.execute("INSERT INTO trades VALUES ('recovered-trade', 'alpha-a', 2.0)")
    with sqlite3.connect(production) as conn:
        conn.execute("UPDATE trades SET pnl = 99.0 WHERE trade_id = 'old-trade'")

    with pytest.raises(DatabaseMergeError, match="table drift"):
        merge_main_database(production, baseline, candidate, ("alpha-a",))

    with sqlite3.connect(production) as conn:
        assert (
            conn.execute(
                "SELECT pnl FROM trades WHERE trade_id = 'old-trade'"
            ).fetchone()[0]
            == 99.0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM trades WHERE trade_id = 'recovered-trade'"
            ).fetchone()[0]
            == 0
        )
