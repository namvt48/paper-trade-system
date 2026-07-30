#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "numpy>=2.0",
#     "pandas>=2.2",
#     "pyarrow>=14.0",
#     "PyYAML>=6.0",
#     "redis[hiredis]>=5.0",
#     "rich>=13.0",
#     "typer>=0.12",
# ]
# ///

# ─── How to run ───
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Inspect commands without performing recovery:
#      uv run scripts/recover_rebalances.py --help
# 3. Build only staged artifacts:
#      uv run scripts/recover_rebalances.py build --help
# 4. Promote only after manual review and service shutdown:
#      uv run scripts/recover_rebalances.py promote --help
# ──────────────────

"""CLI for staged rebalance recovery; build is read-only and promote is guarded."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import typer
from rich.console import Console

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "alphas"))

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command()
def build(
    run_id: str = typer.Option(...),
    source_main: Path = typer.Option(..., exists=True, dir_okay=False),
    source_equity: Path = typer.Option(..., exists=True, dir_okay=False),
    runner_config: Path = typer.Option(..., exists=True, dir_okay=False),
    alphas_dir: Path = typer.Option(Path("alphas"), exists=True, file_okay=False),
    mds_cache: Path = typer.Option(..., exists=True, file_okay=False),
    mds_redis_url: str = typer.Option(..., envvar="MDS_REDIS_URL"),
    recovery_root: Path = typer.Option(Path("recovery")),
    start: str = typer.Option("2026-07-16"),
    end: str = typer.Option("2026-07-17"),
    only_alpha: list[str] = typer.Option(
        None,
        "--only-alpha",
        help="Restrict recovery to these alpha_ids (repeatable). "
        "Omit to use the full frozen incident schedule.",
    ),
    close_at: str | None = typer.Option(
        None,
        "--close-at",
        help="Explicit UTC candle-close timestamp for a known missed cycle. "
        "Requires at least one --only-alpha.",
    ),
) -> None:
    """Capture inputs and build validated candidates without writing production."""
    from scripts.rebalance_recovery.storage import RunId
    from scripts.rebalance_recovery.workflow import BuildRequest, build_staged_recovery

    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as exc:
        raise typer.BadParameter("start and end must use YYYY-MM-DD") from exc
    if start_date > end_date:
        raise typer.BadParameter("start must be on or before end")
    explicit_close = None
    if close_at is not None:
        if not only_alpha:
            raise typer.BadParameter("--close-at requires --only-alpha")
        try:
            explicit_close = datetime.fromisoformat(
                close_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise typer.BadParameter(
                "--close-at must use an ISO-8601 timestamp"
            ) from exc
    result = build_staged_recovery(
        BuildRequest(
            run_id=RunId(run_id),
            recovery_root=recovery_root,
            source_main=source_main,
            source_equity=source_equity,
            runner_config=runner_config,
            alphas_dir=alphas_dir,
            mds_cache=mds_cache,
            mds_redis_url=mds_redis_url,
            start=start_date,
            end=end_date,
            only_alphas=tuple(only_alpha or ()),
            close_at=explicit_close,
        )
    )
    status = "PASS" if result.validation_passed else "FAIL"
    console.print(f"validation={status}")
    console.print(f"workspace={result.workspace.root}")
    console.print(f"approval_hash={result.approval_hash}")
    if not result.validation_passed:
        raise typer.Exit(code=2)


@app.command()
def promote(
    workspace: Path = typer.Option(..., exists=True, file_okay=False),
    production_main: Path = typer.Option(..., exists=True, dir_okay=False),
    production_equity: Path = typer.Option(..., exists=True, dir_okay=False),
    redis_url: str = typer.Option(..., envvar="REDIS_URL"),
    approval_hash: str = typer.Option(...),
    services_stopped: bool = typer.Option(False, "--services-stopped"),
) -> None:
    """Merge a reviewed workspace after all runtime writers stop."""
    from scripts.rebalance_recovery.promotion import promote_validated_workspace

    promote_validated_workspace(
        workspace=workspace,
        production_main=production_main,
        production_equity=production_equity,
        redis_url=redis_url,
        approval_hash=approval_hash,
        services_stopped=services_stopped,
    )
    console.print("promotion=COMPLETE")


if __name__ == "__main__":
    app()
