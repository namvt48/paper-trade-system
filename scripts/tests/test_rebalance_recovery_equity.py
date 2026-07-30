from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.rebalance_recovery.candidate import build_main_candidate
from scripts.rebalance_recovery.configuration import RecoveryAlphaConfig
from scripts.rebalance_recovery.domain import (
    AlphaId,
    CandleOpenMs,
    CloseLedgerEntry,
    OpenLedgerEntry,
    PositionId,
    Side,
    SignalId,
)
from scripts.rebalance_recovery.equity import build_equity_candidate
from scripts.rebalance_recovery.ledger import read_ledger, write_ledger


def _create_main_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE positions (
                position_id TEXT PRIMARY KEY, alpha_id TEXT, signal_id TEXT,
                symbol TEXT, side TEXT, entry_price REAL, qty REAL, tp REAL,
                sl REAL, leverage INTEGER, opened_at TEXT, metadata TEXT,
                exchange TEXT, fee_pct REAL
            );
            CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY, position_id TEXT, alpha_id TEXT,
                signal_id TEXT, symbol TEXT, side TEXT, entry_price REAL,
                exit_price REAL, qty REAL, pnl REAL, pnl_percent REAL,
                leverage INTEGER, tp REAL, sl REAL, reason TEXT,
                duration_hours REAL, opened_at TEXT, closed_at TEXT,
                metadata TEXT, fee REAL, exchange TEXT
            );
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY, signal_id TEXT, alpha_id TEXT,
                type TEXT, payload TEXT, received_at TEXT, processed INTEGER,
                error TEXT
            );
            """
        )
        conn.execute(
            """INSERT INTO positions
               (position_id, alpha_id, signal_id, symbol, side, entry_price,
                qty, leverage, opened_at, metadata, exchange, fee_pct)
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


def test_ledger_round_trip_preserves_typed_variants(tmp_path: Path) -> None:
    event_at = datetime(2026, 7, 16, 0, 0, 5, tzinfo=timezone.utc)
    entries = (
        CloseLedgerEntry(
            AlphaId("alpha"),
            SignalId("close"),
            PositionId("old"),
            "BTCUSDT",
            Side.LONG,
            101.0,
            CandleOpenMs(1000),
            event_at,
            0.0007,
        ),
        OpenLedgerEntry(
            AlphaId("alpha"),
            SignalId("open"),
            PositionId("new"),
            "ETHUSDT",
            Side.SHORT,
            50.0,
            2.0,
            -0.5,
            1.0,
            CandleOpenMs(1000),
            event_at,
            0.0007,
        ),
    )
    path = tmp_path / "ledger.jsonl"

    write_ledger(path, entries)

    assert read_ledger(path) == entries


def test_equity_rebuild_only_mutates_candidate(tmp_path: Path) -> None:
    main_source = tmp_path / "main-source.db"
    main_candidate = tmp_path / "main-candidate.db"
    equity_source = tmp_path / "equity-source.db"
    equity_candidate = tmp_path / "equity-candidate.db"
    market_root = tmp_path / "market"
    _create_main_db(main_source)
    event_at = datetime(2026, 7, 16, 0, 0, 5, tzinfo=timezone.utc)
    ledger = (
        CloseLedgerEntry(
            AlphaId("15m-blend-close"),
            SignalId("close-signal"),
            PositionId("old-position"),
            "BTCUSDT",
            Side.LONG,
            110.0,
            CandleOpenMs(1_784_156_700_000),
            event_at,
            0.0007,
        ),
    )
    build_main_candidate(main_source, main_candidate, ledger, recovered_at=event_at)
    with sqlite3.connect(equity_source) as conn:
        conn.execute(
            """CREATE TABLE equity_snapshots (
                id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
                alpha_id TEXT NOT NULL, balance REAL NOT NULL,
                unrealized_pnl REAL DEFAULT 0, realized_pnl REAL DEFAULT 0
            )"""
        )
        timestamp = "2026-07-16T00:20:00+00:00"
        conn.execute(
            "INSERT INTO equity_snapshots VALUES (1, ?, ?, 1, 0, 0)",
            (timestamp, "15m-blend-close"),
        )
        conn.execute(
            "INSERT INTO equity_snapshots VALUES (2, ?, ?, 1, 0, 0)",
            (timestamp, "__TOTAL__"),
        )
    (market_root / "equity-15m").mkdir(parents=True)
    rows = [
        {
            "open_time": int(
                datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
            ),
            "close": 110.0,
        }
    ]
    (market_root / "equity-15m" / "BTCUSDT.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )
    config = RecoveryAlphaConfig(
        AlphaId("15m-blend-close"),
        "1",
        tmp_path / "spec.json",
        tmp_path / "whitelist.txt",
        None,
        1,
        10_000.0,
        "binance",
    )
    source_hash = hashlib.sha256(equity_source.read_bytes()).hexdigest()

    build_equity_candidate(
        equity_source,
        equity_candidate,
        main_candidate,
        market_root,
        (config,),
        ledger,
    )

    assert hashlib.sha256(equity_source.read_bytes()).hexdigest() == source_hash
    with sqlite3.connect(equity_candidate) as conn:
        alpha_balance = conn.execute(
            "SELECT balance FROM equity_snapshots WHERE alpha_id = '15m-blend-close'"
        ).fetchone()[0]
        total_balance = conn.execute(
            "SELECT balance FROM equity_snapshots WHERE alpha_id = '__TOTAL__'"
        ).fetchone()[0]
    assert alpha_balance == pytest.approx(total_balance)
    assert alpha_balance != 1.0
