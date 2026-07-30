from __future__ import annotations

from typer.testing import CliRunner

from scripts.recover_rebalances import app


def test_build_help_accepts_incident_date_options() -> None:
    result = CliRunner().invoke(app, ["build", "--help"])

    assert result.exit_code == 0, repr(result.exception)
