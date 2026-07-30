"""Produce machine- and human-readable evidence before promotion is unlocked."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .configuration import RecoveryAlphaConfig
from .domain import CloseLedgerEntry, LedgerEntry, RecoveryPoint
from .scope import EXCLUDED_ALPHA
from .storage import write_json


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    """One named invariant and its evidence summary."""

    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Promotion is allowed only when every contained check passes."""

    checks: tuple[ValidationCheck, ...]

    @property
    def passed(self) -> bool:
        """Collapse all invariant outcomes into the promotion gate."""
        return all(check.passed for check in self.checks)


def validate_candidates(
    baseline_main: Path,
    candidate_main: Path,
    candidate_equity: Path,
    ledger: tuple[LedgerEntry, ...],
    configs: tuple[RecoveryAlphaConfig, ...],
    points: tuple[RecoveryPoint, ...],
) -> ValidationReport:
    """Run integrity, isolation, uniqueness, ledger, and equity invariants."""
    checks = (
        _integrity_check(candidate_main, "main.integrity"),
        _integrity_check(candidate_equity, "equity.integrity"),
        _excluded_alpha_check(baseline_main, candidate_main),
        _cycle_check(ledger, points),
        _ledger_signal_check(candidate_main, ledger),
        _duplicate_position_check(candidate_main),
        _duplicate_signal_check(candidate_main, ledger),
        _duration_check(candidate_main, ledger),
        _equity_formula_check(candidate_equity, configs, ledger),
        _total_equity_check(candidate_equity, ledger),
    )
    return ValidationReport(checks=checks)


def write_validation_report(
    report: ValidationReport, json_path: Path, markdown_path: Path
) -> None:
    """Persist the exact evidence reviewed before supplying an approval hash."""
    write_json(
        json_path,
        {"passed": report.passed, "checks": [asdict(check) for check in report.checks]},
    )
    lines = [
        "# Rebalance recovery validation",
        "",
        f"Overall: {'PASS' if report.passed else 'FAIL'}",
        "",
    ]
    lines.extend(
        f"- {'PASS' if check.passed else 'FAIL'} `{check.name}` — {check.evidence}"
        for check in report.checks
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _integrity_check(path: Path, name: str) -> ValidationCheck:
    """Run SQLite's structural integrity check."""
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    return ValidationCheck(name=name, passed=result == "ok", evidence=str(result))


def _excluded_alpha_check(baseline: Path, candidate: Path) -> ValidationCheck:
    """Prove the excluded alpha's rows are byte-logically unchanged."""
    before = _alpha_digest(baseline, str(EXCLUDED_ALPHA))
    after = _alpha_digest(candidate, str(EXCLUDED_ALPHA))
    return ValidationCheck(
        name="excluded.1d-trend60cmf",
        passed=before == after,
        evidence=f"before={before} after={after}",
    )


def _alpha_digest(path: Path, alpha_id: str) -> str:
    """Hash canonical rows for one alpha across all affected main tables."""
    digest = hashlib.sha256()
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for table in ("positions", "trades", "signals"):
            rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} WHERE alpha_id = ? ORDER BY 1", (alpha_id,)
                )
            ]
            digest.update(
                json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
    return digest.hexdigest()


def _ledger_signal_check(
    path: Path, ledger: tuple[LedgerEntry, ...]
) -> ValidationCheck:
    """Require every reviewed ledger signal to exist and be processed once."""
    expected = {str(entry.signal_id) for entry in ledger}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        actual = (
            {
                row[0]
                for row in conn.execute(
                    "SELECT signal_id FROM signals WHERE signal_id IN (%s) AND processed = 1"
                    % ",".join("?" for _ in expected),
                    tuple(expected),
                )
            }
            if expected
            else set()
        )
    return ValidationCheck(
        name="ledger.signals",
        passed=actual == expected,
        evidence=f"expected={len(expected)} actual={len(actual)}",
    )


def _cycle_check(
    ledger: tuple[LedgerEntry, ...],
    points: tuple[RecoveryPoint, ...],
) -> ValidationCheck:
    """Require every expected incident cycle and reject unexpected cycles."""
    expected = {(point.alpha_id, point.candle_open_ms) for point in points}
    actual = {(entry.alpha_id, entry.candle_open_ms) for entry in ledger}
    return ValidationCheck(
        name="ledger.cycles",
        passed=actual == expected,
        evidence=f"expected={len(expected)} actual={len(actual)}",
    )


def _duplicate_position_check(path: Path) -> ValidationCheck:
    """Reject more than one open row for the same alpha and symbol."""
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM (SELECT alpha_id, symbol FROM positions GROUP BY alpha_id, symbol HAVING COUNT(*) > 1)"
        ).fetchone()[0]
    return ValidationCheck(
        name="positions.unique_alpha_symbol",
        passed=count == 0,
        evidence=f"duplicates={count}",
    )


def _duplicate_signal_check(
    path: Path, ledger: tuple[LedgerEntry, ...]
) -> ValidationCheck:
    """Reject duplicates among deterministic recovery signal identifiers."""
    expected = {str(entry.signal_id) for entry in ledger}
    if not expected:
        return ValidationCheck(
            name="signals.unique_id", passed=True, evidence="no recovery signals"
        )
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM (SELECT signal_id FROM signals "
            f"WHERE signal_id IN ({','.join('?' for _ in expected)}) "
            "GROUP BY signal_id HAVING COUNT(*) > 1)",
            tuple(expected),
        ).fetchone()[0]
    return ValidationCheck(
        name="signals.unique_id", passed=count == 0, evidence=f"duplicates={count}"
    )


def _duration_check(path: Path, ledger: tuple[LedgerEntry, ...]) -> ValidationCheck:
    """Reject negative or non-finite recovered trade durations."""
    position_ids = {
        str(entry.position_id)
        for entry in ledger
        if isinstance(entry, CloseLedgerEntry)
    }
    if not position_ids:
        return ValidationCheck(
            name="trades.duration", passed=True, evidence="no recovered closes"
        )
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM trades "
            f"WHERE position_id IN ({','.join('?' for _ in position_ids)}) "
            "AND (duration_hours < 0 OR duration_hours != duration_hours)",
            tuple(position_ids),
        ).fetchone()[0]
    return ValidationCheck(
        name="trades.duration", passed=count == 0, evidence=f"negative={count}"
    )


def _equity_formula_check(
    path: Path,
    configs: tuple[RecoveryAlphaConfig, ...],
    ledger: tuple[LedgerEntry, ...],
) -> ValidationCheck:
    """Verify balance equals capital plus realized and unrealized PnL."""
    capitals = {str(config.alpha_id): config.capital for config in configs}
    affected = {str(entry.alpha_id) for entry in ledger}
    start = min(entry.event_at for entry in ledger).isoformat() if ledger else ""
    failures = 0
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        for alpha_id in affected:
            capital = capitals.get(alpha_id, 10_000.0)
            failures += conn.execute(
                """SELECT COUNT(*) FROM equity_snapshots
                   WHERE alpha_id = ? AND timestamp >= ?
                     AND ABS(balance - ? - realized_pnl - unrealized_pnl) > 0.000001""",
                (alpha_id, start, capital),
            ).fetchone()[0]
    return ValidationCheck(
        name="equity.formula", passed=failures == 0, evidence=f"failures={failures}"
    )


def _total_equity_check(path: Path, ledger: tuple[LedgerEntry, ...]) -> ValidationCheck:
    """Verify each rebuilt total equals all per-alpha balances at that timestamp."""
    if not ledger:
        return ValidationCheck(
            name="equity.total", passed=True, evidence="no ledger entries"
        )
    start = min(entry.event_at for entry in ledger).isoformat()
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        failures = conn.execute(
            """SELECT COUNT(*) FROM equity_snapshots total
               WHERE total.alpha_id = '__TOTAL__' AND total.timestamp >= ?
                 AND ABS(total.balance - (
                   SELECT COALESCE(SUM(item.balance), 0) FROM equity_snapshots item
                   WHERE item.timestamp = total.timestamp AND item.alpha_id != '__TOTAL__'
                 )) > 0.000001""",
            (start,),
        ).fetchone()[0]
    return ValidationCheck(
        name="equity.total", passed=failures == 0, evidence=f"failures={failures}"
    )
