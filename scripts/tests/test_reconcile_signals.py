from __future__ import annotations

import sqlite3

from scripts import reconcile_signals as rs


# ---- pure reconcile() ----

def test_reconcile_ok_when_all_accounted():
    db = {"signals_committed": 8, "signals_errored": 2}
    stream = {"xlen": 12, "pending": 1, "lag": 1}
    result = rs.reconcile(db, stream, tolerance=0)
    assert result.gap == 0
    assert result.ok is True


def test_reconcile_flags_positive_gap_as_silent_drop():
    # published 20, but only 10 committed + 2 errored + 1 pending + 1 lag = 14 → gap 6
    db = {"signals_committed": 10, "signals_errored": 2}
    stream = {"xlen": 20, "pending": 1, "lag": 1}
    result = rs.reconcile(db, stream, tolerance=0)
    assert result.gap == 6
    assert result.ok is False


def test_reconcile_pending_and_lag_are_not_counted_as_loss():
    # everything either committed or still in flight → no gap
    db = {"signals_committed": 5, "signals_errored": 0}
    stream = {"xlen": 100, "pending": 30, "lag": 65}
    result = rs.reconcile(db, stream, tolerance=0)
    assert result.gap == 0
    assert result.ok is True


def test_reconcile_tolerance_absorbs_small_gap():
    db = {"signals_committed": 98, "signals_errored": 0}
    stream = {"xlen": 100, "pending": 0, "lag": 0}
    assert rs.reconcile(db, stream, tolerance=0).ok is False
    assert rs.reconcile(db, stream, tolerance=5).ok is True


# ---- collect_db_stats against a real temp SQLite ----

def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT,
            alpha_id TEXT, type TEXT, payload TEXT, received_at TEXT,
            processed INTEGER DEFAULT 0, error TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY);
        CREATE TABLE positions (id INTEGER PRIMARY KEY);
        """
    )
    rows = [
        ("s1", "a", "OPEN", "2026-07-17T01:00:00", 1, None),
        ("s2", "a", "CLOSE", "2026-07-17T02:00:00", 1, None),
        ("s3", "b", "OPEN", "2026-07-16T23:00:00", 1, "boom"),   # errored, before since
        ("s4", "b", "OPEN", "2026-07-17T03:00:00", 1, "boom"),   # errored, after since
    ]
    conn.executemany(
        "INSERT INTO signals (signal_id, alpha_id, type, received_at, processed, error) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO trades DEFAULT VALUES")
    conn.execute("INSERT INTO positions DEFAULT VALUES")
    conn.commit()
    conn.close()


def test_collect_db_stats_counts_and_since_filter(tmp_path):
    path = str(tmp_path / "t.db")
    _make_db(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        allstats = rs.collect_db_stats(conn)
        assert allstats["signals_rows"] == 4
        assert allstats["signals_committed"] == 2
        assert allstats["signals_errored"] == 2
        assert allstats["trades"] == 1
        assert allstats["positions"] == 1

        since = rs.collect_db_stats(conn, since="2026-07-17T00:00:00")
        assert since["signals_rows"] == 3          # s3 excluded
        assert since["signals_committed"] == 2
        assert since["signals_errored"] == 1       # only s4
    finally:
        conn.close()


# ---- collect_stream_stats against a fake redis ----

class FakeRedis:
    def __init__(self, xlen, groups):
        self._xlen = xlen
        self._groups = groups

    def xlen(self, stream):
        return self._xlen

    def xinfo_groups(self, stream):
        return self._groups


def test_collect_stream_stats_reads_matching_group():
    fake = FakeRedis(50, [
        {"name": "other", "pending": 9, "lag": 9, "entries-read": 9},
        {"name": "paper-executor", "pending": 3, "lag": 4, "entries-read": 43},
    ])
    stats = rs.collect_stream_stats(fake, "paper-signals", "paper-executor")
    assert stats == {"xlen": 50, "pending": 3, "lag": 4, "entries_read": 43}


# ---- main() exit codes ----

def test_main_returns_0_when_ok(monkeypatch):
    monkeypatch.setattr(rs, "run", lambda *a, **k: rs.reconcile(
        {"signals_committed": 10, "signals_errored": 0}, {"xlen": 10}))
    assert rs.main(["--db", "x", "--redis-url", "y"]) == 0


def test_main_returns_1_when_gap(monkeypatch):
    monkeypatch.setattr(rs, "run", lambda *a, **k: rs.reconcile(
        {"signals_committed": 1, "signals_errored": 0}, {"xlen": 10}))
    assert rs.main(["--db", "x", "--redis-url", "y"]) == 1
