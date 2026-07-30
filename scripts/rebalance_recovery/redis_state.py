"""Derive runner Redis position payloads from the fully validated candidate DB."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .domain import LedgerEntry
from .storage import JsonValue, write_json


def write_candidate_redis_state(
    candidate_main: Path,
    output: Path,
    ledger: tuple[LedgerEntry, ...],
) -> None:
    """Write every affected runner position key, including explicit empty books."""
    affected = sorted({entry.alpha_id for entry in ledger})
    state: dict[str, dict[str, JsonValue]] = {
        f"runner:positions:{alpha_id}": {} for alpha_id in affected
    }
    with sqlite3.connect(f"file:{candidate_main.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for alpha_id in affected:
            for row in conn.execute(
                "SELECT * FROM positions WHERE alpha_id = ? ORDER BY symbol",
                (str(alpha_id),),
            ):
                metadata = json.loads(row["metadata"] or "{}")
                positions = state[f"runner:positions:{alpha_id}"]
                positions[str(row["symbol"])] = {
                    "position_id": row["position_id"],
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "entry": float(metadata.get("decision_price", row["entry_price"])),
                    "qty": float(row["qty"]),
                    "weight": float(metadata.get("weight", 0.0)),
                    "strategy_leverage": float(metadata.get("strategy_leverage", 1.0)),
                    "entry_candle_open_ms": int(
                        metadata.get("entry_candle_open_ms", 0)
                    ),
                }
    write_json(output, state)
