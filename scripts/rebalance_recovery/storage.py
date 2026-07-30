"""Durable workspace, hashing, and SQLite snapshot primitives for recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NewType

RunId = NewType("RunId", str)


@dataclass(frozen=True, slots=True)
class Workspace:
    """Paths owned by one immutable recovery run."""

    root: Path
    inputs: Path
    baseline_main: Path
    baseline_equity: Path
    candidate_main: Path
    candidate_equity: Path
    ledger: Path
    manifest: Path
    report_json: Path
    report_markdown: Path
    redis_state: Path


class WorkspaceError(Exception):
    """Workspace creation or immutable artifact handling failed."""


def create_workspace(recovery_root: Path, run_id: RunId) -> Workspace:
    """Create an empty run directory and refuse accidental reuse."""
    root = recovery_root / str(run_id)
    if root.exists():
        raise WorkspaceError(f"recovery run already exists: {root}")
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    return Workspace(
        root=root,
        inputs=inputs,
        baseline_main=root / "baseline-paper-trade.db",
        baseline_equity=root / "baseline-equity-snapshots.db",
        candidate_main=root / "candidate-paper-trade.db",
        candidate_equity=root / "candidate-equity-snapshots.db",
        ledger=root / "recovery-events.jsonl",
        manifest=root / "manifest.json",
        report_json=root / "validation-report.json",
        report_markdown=root / "validation-report.md",
        redis_state=root / "candidate-redis-state.json",
    )


def sqlite_snapshot(source: Path, destination: Path) -> None:
    """Create a transactionally consistent SQLite backup from a read-only source."""
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        with sqlite3.connect(destination) as destination_conn:
            source_conn.backup(destination_conn)


def sha256_file(path: Path) -> str:
    """Hash an artifact without loading large database files into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


type JsonValue = (
    str | int | float | bool | None | Sequence[JsonValue] | Mapping[str, JsonValue]
)


def write_json(path: Path, value: JsonValue) -> None:
    """Write canonical JSON so its SHA-256 is stable across reruns."""
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def database_guard(path: Path) -> tuple[tuple[str, str], ...]:
    """Return compact semantic watermarks used to reject a stale promotion."""
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        guards: list[tuple[str, str]] = []
        for table in ("positions", "trades", "signals", "equity_snapshots"):
            if table not in tables:
                continue
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            guards.append((f"{table}.count", str(count)))
        if "signals" in tables:
            latest = conn.execute(
                "SELECT COALESCE(MAX(received_at), '') FROM signals"
            ).fetchone()[0]
            guards.append(("signals.max_received_at", str(latest)))
        if "equity_snapshots" in tables:
            latest = conn.execute(
                "SELECT COALESCE(MAX(timestamp), '') FROM equity_snapshots"
            ).fetchone()[0]
            guards.append(("equity.max_timestamp", str(latest)))
        return tuple(sorted(guards))
