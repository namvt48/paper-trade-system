from __future__ import annotations

import json
from datetime import datetime, timezone


def parse_snapshot(raw: str | bytes | None) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), list):
        return None
    return payload


def snapshot_age_sec(snapshot: dict, now=None) -> float:
    now = now or datetime.now(timezone.utc)
    try:
        generated = datetime.fromisoformat(
            str(snapshot["generated_at"]).replace("Z", "+00:00")
        )
        return max(0.0, (now - generated).total_seconds())
    except (KeyError, TypeError, ValueError):
        return float("inf")


def normalize_position(position: dict) -> dict:
    metadata = position.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    result = {
        "position_id": str(position["position_id"]),
        "alpha_id": str(position.get("alpha_id", "")),
        "symbol": str(position["symbol"]),
        "side": str(position["side"]).upper(),
        "entry": float(position["entry_price"]),
        "entry_price": float(position["entry_price"]),
        "qty": float(position["qty"]),
        "tp": float(position["tp"]) if position.get("tp") is not None else None,
        "sl": float(position["sl"]) if position.get("sl") is not None else None,
        "leverage": int(position.get("leverage") or 1),
        "opened_at": position.get("opened_at"),
        "exchange": position.get("exchange") or "binance",
        "metadata": metadata,
    }
    runtime = metadata.get("strategy_runtime")
    if isinstance(runtime, dict):
        result.update(runtime)
    trail_distance = result.get("trail_distance", metadata.get("trail_distance"))
    if trail_distance is not None:
        result["trail_distance"] = float(trail_distance)
        # Legacy recovery derives extrema from the authoritative stop without
        # moving that stop. Strategies can tighten it later from fresh prices.
        if result["side"] == "LONG":
            result.setdefault("hse", (result["sl"] or result["entry"]) + float(trail_distance))
            result.setdefault("lse", result["entry"])
        else:
            result.setdefault("hse", result["entry"])
            result.setdefault("lse", (result["sl"] or result["entry"]) - float(trail_distance))
    result.setdefault("size", float(metadata.get("trade_size", result["entry"] * result["qty"])))
    result.setdefault("initial_qty", result["qty"])
    result.setdefault("remaining_qty", result["qty"])
    result.setdefault("tp_hits", 2 if not isinstance(runtime, dict) else 0)
    result.setdefault("be_active", False)
    result.setdefault("last_tp_signal_bar_time", None)
    result.setdefault("entry_candle_open_ms", 0)
    result.setdefault("signal_candle_close_ms", 0)
    result.setdefault("last_managed_ms", result["signal_candle_close_ms"])
    result.setdefault("post_hold_sl", result["sl"])
    result.setdefault("post_hold_tp", result["tp"])
    result.setdefault("post_hold_tpsl_activated", True)
    result.setdefault("bar_count", 0)
    result.setdefault("be_activated", False)
    return result
