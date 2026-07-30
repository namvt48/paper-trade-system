"""Orchestrate immutable capture, replay, candidate builds, and validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import cast

import redis

from .candidate import build_main_candidate
from .configuration import load_recovery_configs
from .equity import build_equity_candidate
from .ledger import write_ledger
from .market import RedisListReader, capture_market_inputs
from .redis_state import write_candidate_redis_state
from .replay import build_recovery_ledger
from .scope import build_close_points, build_incident_points
from .storage import (
    RunId,
    Workspace,
    create_workspace,
    database_guard,
    sha256_file,
    sqlite_snapshot,
    write_json,
)
from .validation import validate_candidates, write_validation_report


@dataclass(frozen=True, slots=True)
class BuildRequest:
    """All explicit source and destination boundaries for one staged build."""

    run_id: RunId
    recovery_root: Path
    source_main: Path
    source_equity: Path
    runner_config: Path
    alphas_dir: Path
    mds_cache: Path
    mds_redis_url: str
    start: date
    end: date
    only_alphas: tuple[str, ...] = ()
    close_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Artifacts and approval hash returned after validation completes."""

    workspace: Workspace
    validation_passed: bool
    approval_hash: str


def build_staged_recovery(request: BuildRequest) -> BuildResult:
    """Build a complete recovery workspace while production remains read-only."""
    workspace = create_workspace(request.recovery_root, request.run_id)
    sqlite_snapshot(request.source_main, workspace.baseline_main)
    sqlite_snapshot(request.source_equity, workspace.baseline_equity)
    configs = load_recovery_configs(request.runner_config, request.alphas_dir)
    points = (
        build_close_points(request.close_at, request.only_alphas)
        if request.close_at is not None
        else build_incident_points(
            request.start,
            request.end,
            only_alphas=request.only_alphas or None,
        )
    )
    with redis.Redis.from_url(
        request.mds_redis_url,
        socket_timeout=10.0,
        socket_connect_timeout=5.0,
    ) as redis_reader:
        market_files = capture_market_inputs(
            workspace,
            configs,
            points,
            request.mds_cache,
            cast(RedisListReader, cast(object, redis_reader)),
        )
    ledger = build_recovery_ledger(
        workspace.baseline_main,
        workspace.inputs / "market",
        request.alphas_dir,
        configs,
        points,
    )
    write_ledger(workspace.ledger, ledger)
    recovered_at = datetime.now(timezone.utc)
    build_main_candidate(
        workspace.baseline_main,
        workspace.candidate_main,
        ledger,
        recovered_at,
    )
    build_equity_candidate(
        workspace.baseline_equity,
        workspace.candidate_equity,
        workspace.candidate_main,
        workspace.inputs / "market",
        configs,
        ledger,
    )
    write_candidate_redis_state(workspace.candidate_main, workspace.redis_state, ledger)
    report = validate_candidates(
        workspace.baseline_main,
        workspace.candidate_main,
        workspace.candidate_equity,
        ledger,
        configs,
        points,
    )
    write_validation_report(report, workspace.report_json, workspace.report_markdown)
    manifest_value = {
        "run_id": str(request.run_id),
        "created_at": recovered_at.isoformat(),
        "window": {
            "start": request.start.isoformat(),
            "end": request.end.isoformat(),
            "only_alphas": list(request.only_alphas),
            "close_at": request.close_at.isoformat()
            if request.close_at is not None
            else None,
        },
        "source_guards": {
            "main": list(database_guard(workspace.baseline_main)),
            "equity": list(database_guard(workspace.baseline_equity)),
        },
        "points": [
            {
                "alpha_id": str(point.alpha_id),
                "timeframe": point.timeframe,
                "candle_open_ms": int(point.candle_open_ms),
                "event_at": point.event_at.isoformat(),
            }
            for point in points
        ],
        "configs": [
            {
                "alpha_id": str(config.alpha_id),
                "version": config.version,
                "spec_path": str(config.spec_path),
                "whitelist_path": str(config.whitelist_path),
                "blacklist_path": str(config.blacklist_path)
                if config.blacklist_path
                else None,
                "warmup_bars": config.warmup_bars,
                "capital": config.capital,
                "exchange": config.exchange,
                "spec_sha256": sha256_file(config.spec_path),
                "whitelist_sha256": sha256_file(config.whitelist_path),
            }
            for config in configs
        ],
        "artifacts": {
            "ledger": sha256_file(workspace.ledger),
            "candidate_main": sha256_file(workspace.candidate_main),
            "candidate_equity": sha256_file(workspace.candidate_equity),
            "redis_state": sha256_file(workspace.redis_state),
            "validation_report": sha256_file(workspace.report_json),
        },
        "market_inputs": {
            str(path.relative_to(workspace.root)): sha256_file(path)
            for path in market_files
        },
        "validation_passed": report.passed,
        "tradability_policy": "whitelist_price_coverage_fail_open",
    }
    write_json(workspace.manifest, manifest_value)
    approval_hash = sha256_file(workspace.manifest)
    return BuildResult(
        workspace=workspace,
        validation_passed=report.passed,
        approval_hash=approval_hash,
    )
