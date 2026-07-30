from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .blend import BlendResult, build_blend_outputs
from .book import TargetBookStore


_SUPPORTED_ON_STALE = {"drop_to_flat"}


@dataclass(frozen=True)
class SleeveConfig:
    sleeve_id: str
    weight: float
    max_staleness_sec: float
    on_stale: str = "drop_to_flat"

    def __post_init__(self) -> None:
        # hold_last/halt are valid config values (see PORTFOLIO_MANAGER_DESIGN.md
        # R5) but not yet implemented in blend_books, which always drops a
        # stale sleeve from the blend. Reject them here instead of silently
        # behaving like drop_to_flat -- a config author asking for hold_last
        # must not get drop_to_flat without being told.
        if self.on_stale not in _SUPPORTED_ON_STALE:
            raise ValueError(
                f"{self.sleeve_id}: on_stale={self.on_stale!r} is not implemented "
                f"(supported: {sorted(_SUPPORTED_ON_STALE)})"
            )


class PortfolioEngine:
    """Pure-cycle PM coordinator; signal publishing is injected by the caller."""

    def __init__(self, config: dict[str, Any], redis_client: Any) -> None:
        self.config = config
        self.store = TargetBookStore(redis_client)
        self.previous: dict[str, float] = {}
        # Validated eagerly (fail loud at construction, not on the first
        # cycle) so an unsupported on_stale value in portfolio.json is
        # caught before PM starts blending, not silently mid-run.
        self.sleeves = tuple(
            SleeveConfig(
                sleeve_id=str(item["id"]),
                weight=float(item["weight"]),
                max_staleness_sec=float(item["max_staleness_sec"]),
                on_stale=str(item.get("on_stale", "drop_to_flat")),
            )
            for item in config["sleeves"]
        )

    @classmethod
    def from_json(cls, path: str | Path, redis_client: Any) -> "PortfolioEngine":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), redis_client)

    def cycle(
        self,
        *,
        regime_state: dict[str, float | bool] | None = None,
        now: float | None = None,
    ) -> BlendResult:
        books = {
            sleeve.sleeve_id: self.store.read(sleeve.sleeve_id)
            for sleeve in self.sleeves
        }
        allocations = {sleeve.sleeve_id: sleeve.weight for sleeve in self.sleeves}
        max_staleness = {
            sleeve.sleeve_id: sleeve.max_staleness_sec for sleeve in self.sleeves
        }
        overlay = self.config.get("overlay", {})
        result = build_blend_outputs(
            books,
            allocations,
            max_staleness,
            cap=float(overlay.get("per_coin_cap", 1.0)),
            gross=float(overlay.get("gross_target", 1.0)),
            regime_state=regime_state,
            downtrend_multiplier=float(overlay.get("downtrend_multiplier", 1.0)),
            previous=self.previous,
            ema_span=int(overlay["ema_span"]) if overlay.get("ema_span") else None,
            now=now,
        )
        self.previous = dict(result.baseline)
        return result
