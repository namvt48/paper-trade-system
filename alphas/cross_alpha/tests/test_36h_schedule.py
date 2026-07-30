from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from cross_alpha.schedule import is_rebalance_due


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TIMEFRAME_MS = {
    "15m": 15 * 60 * 1_000,
    "1h": 60 * 60 * 1_000,
}


def _is_due(spec: dict[str, object], candle_open_ms: int) -> bool:
    timeframe = str(spec["timeframe"])
    timeframe_ms = TIMEFRAME_MS[timeframe]
    return is_rebalance_due(
        candle_open_ms,
        timeframe_ms,
        int(spec["rebalance_bars"]),
        publish_at_midnight_utc=bool(spec.get("publish_at_midnight_utc")),
        rebalance_on_close=bool(spec.get("rebalance_on_close")),
    )


def test_enabled_36h_alphas_rebalance_every_36_hours() -> None:
    config = yaml.safe_load(
        (REPOSITORY_ROOT / "runner-config.production.yaml").read_text(
            encoding="utf-8"
        )
    )
    enabled_36h = [
        item
        for item in config["alphas"]
        if item.get("enabled", True) and "36h" in item["alpha_id"]
    ]
    assert enabled_36h

    start = datetime(2026, 7, 24, tzinfo=timezone.utc)
    end = start + timedelta(days=4)
    for alpha in enabled_36h:
        spec = json.loads(
            (REPOSITORY_ROOT / "alphas" / alpha["params"]["spec_file"]).read_text(
                encoding="utf-8"
            )
        )
        timeframe_ms = TIMEFRAME_MS[str(spec["timeframe"])]
        candle = int(start.timestamp() * 1_000) - timeframe_ms
        due_closes: list[int] = []
        while candle < int(end.timestamp() * 1_000):
            if _is_due(spec, candle):
                due_closes.append(candle + timeframe_ms)
            candle += timeframe_ms

        assert len(due_closes) >= 2, alpha["alpha_id"]
        intervals = [
            (right - left) / 3_600_000
            for left, right in zip(due_closes, due_closes[1:])
        ]
        assert set(intervals) == {36.0}, (
            f"{alpha['alpha_id']} effective cadence is {intervals}, expected 36h"
        )
