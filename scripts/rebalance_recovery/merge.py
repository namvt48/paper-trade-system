"""Merge a validated recovery candidate into unchanged production databases."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path


class DatabaseMergeError(Exception):
    """A database no longer matches the guarded recovery inputs."""


def merge_main_database(
    production: Path,
    baseline: Path,
    candidate: Path,
    affected_alphas: Sequence[str],
) -> None:
    """Append recovered history and replace only affected current positions."""
    if not affected_alphas:
        raise DatabaseMergeError("the affected alpha set is empty")
    with _open_merge(production, baseline, candidate) as conn:
        _begin(conn)
        for table in ("positions", "trades", "signals"):
            _assert_tables_equal(conn, "main", "baseline", table)
        _insert_candidate_delta(conn, "trades")
        _insert_candidate_delta(conn, "signals")
        placeholders = ",".join("?" for _ in affected_alphas)
        conn.execute(
            f"DELETE FROM positions WHERE alpha_id IN ({placeholders})",
            tuple(affected_alphas),
        )
        columns = _columns(conn, "main", "positions")
        names = _column_list(columns)
        conn.execute(
            f"INSERT INTO positions ({names}) "
            f"SELECT {names} FROM candidate.positions "
            f"WHERE alpha_id IN ({placeholders})",
            tuple(affected_alphas),
        )
        for table in ("positions", "trades", "signals"):
            _assert_tables_equal(conn, "main", "candidate", table)


def merge_equity_database(
    production: Path,
    baseline: Path,
    candidate: Path,
) -> None:
    """Update candidate equity values without inserting or deleting snapshots."""
    with _open_merge(production, baseline, candidate) as conn:
        _begin(conn)
        _assert_tables_equal(conn, "main", "baseline", "equity_snapshots")
        conn.execute(
            """
            UPDATE equity_snapshots AS production
            SET balance = candidate.balance,
                unrealized_pnl = candidate.unrealized_pnl,
                realized_pnl = candidate.realized_pnl
            FROM candidate.equity_snapshots AS candidate
            JOIN baseline.equity_snapshots AS baseline ON baseline.id = candidate.id
            WHERE production.id = candidate.id
              AND (
                candidate.balance IS NOT baseline.balance
                OR candidate.unrealized_pnl IS NOT baseline.unrealized_pnl
                OR candidate.realized_pnl IS NOT baseline.realized_pnl
              )
            """
        )
        _assert_tables_equal(conn, "main", "candidate", "equity_snapshots")


def _open_merge(
    production: Path,
    baseline: Path,
    candidate: Path,
) -> sqlite3.Connection:
    """Open production for writes and attach immutable recovery inputs."""
    conn = sqlite3.connect(f"{production.resolve().as_uri()}?mode=rw", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("ATTACH DATABASE ? AS baseline", (_readonly_uri(baseline),))
        conn.execute("ATTACH DATABASE ? AS candidate", (_readonly_uri(candidate),))
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _readonly_uri(path: Path) -> str:
    """Return a SQLite URI that cannot mutate a staged input database."""
    return f"{path.resolve().as_uri()}?mode=ro"


def _begin(conn: sqlite3.Connection) -> None:
    """Acquire the production write lock before checking its baseline."""
    conn.execute("BEGIN IMMEDIATE")


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> tuple[str, ...]:
    """Read and validate the fixed table's column names."""
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    columns = tuple(str(row[1]) for row in rows)
    if not columns:
        raise DatabaseMergeError(f"missing table: {schema}.{table}")
    return columns


def _column_list(columns: Sequence[str]) -> str:
    """Quote column identifiers obtained from SQLite schema metadata."""
    return ", ".join(f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns)


def _assert_tables_equal(
    conn: sqlite3.Connection,
    left_schema: str,
    right_schema: str,
    table: str,
) -> None:
    """Require two primary-keyed tables to contain exactly the same rows."""
    left_columns = _columns(conn, left_schema, table)
    right_columns = _columns(conn, right_schema, table)
    if left_columns != right_columns:
        raise DatabaseMergeError(f"schema mismatch for {table}")
    columns = _column_list(left_columns)
    for source, target in ((left_schema, right_schema), (right_schema, left_schema)):
        difference = conn.execute(
            f"SELECT COUNT(*) FROM ("
            f"SELECT {columns} FROM {source}.{table} EXCEPT "
            f"SELECT {columns} FROM {target}.{table})"
        ).fetchone()
        if difference is None or int(difference[0]) != 0:
            raise DatabaseMergeError(
                f"table drift: {source}.{table} differs from {target}.{table}"
            )


def _insert_candidate_delta(conn: sqlite3.Connection, table: str) -> None:
    """Append rows present in the candidate but absent from the baseline."""
    columns = _columns(conn, "main", table)
    if columns != _columns(conn, "baseline", table):
        raise DatabaseMergeError(f"schema mismatch for {table}")
    if columns != _columns(conn, "candidate", table):
        raise DatabaseMergeError(f"schema mismatch for {table}")
    names = _column_list(columns)
    conn.execute(
        f"INSERT INTO {table} ({names}) "
        f"SELECT {names} FROM candidate.{table} EXCEPT "
        f"SELECT {names} FROM baseline.{table}"
    )
