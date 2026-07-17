from __future__ import annotations

from typing import Any

from runner.strategies.legacy_standalone.strategy import LegacyStandaloneRunnerStrategy


class BangocV22RunnerStrategy(LegacyStandaloneRunnerStrategy):
    """Passes the runner's stale-data gate into the isolated bangoc-v2.2 engine."""

    def __init__(self, alpha_id: str, version: str, params: dict[str, Any], ctx: Any) -> None:
        super().__init__(alpha_id, version, params, ctx)
        set_runner_entry_gate = getattr(self.engine, "set_runner_entry_gate")
        set_runner_entry_gate(self.ctx.can_open_trades)
