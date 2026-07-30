from __future__ import annotations

import math
from collections.abc import Mapping


def per_coin_cap(weights: Mapping[str, float], cap: float) -> dict[str, float]:
    if not math.isfinite(float(cap)) or cap < 0:
        raise ValueError("cap must be finite and non-negative")
    return {symbol: max(-cap, min(cap, float(weight))) for symbol, weight in weights.items() if weight}


def gross_target(weights: Mapping[str, float], target: float) -> dict[str, float]:
    if target < 0 or not math.isfinite(float(target)):
        raise ValueError("target gross must be finite and non-negative")
    gross = sum(abs(float(value)) for value in weights.values())
    if gross == 0:
        return {}
    factor = float(target) / gross
    return {symbol: float(value) * factor for symbol, value in weights.items() if value}


def ema_smooth(
    weights: Mapping[str, float],
    previous: Mapping[str, float] | None,
    span: int,
) -> dict[str, float]:
    if span < 1:
        raise ValueError("EMA span must be positive")
    if not previous:
        return {symbol: float(value) for symbol, value in weights.items() if value}
    alpha = 2.0 / (float(span) + 1.0)
    symbols = set(weights) | set(previous)
    return {
        symbol: alpha * float(weights.get(symbol, 0.0)) + (1.0 - alpha) * float(previous.get(symbol, 0.0))
        for symbol in symbols
        if alpha * float(weights.get(symbol, 0.0)) + (1.0 - alpha) * float(previous.get(symbol, 0.0))
    }


def regime_throttle(
    weights: Mapping[str, float],
    state: Mapping[str, float | bool],
    *,
    downtrend_multiplier: float,
) -> dict[str, float]:
    if not 0 <= float(downtrend_multiplier) <= 1:
        raise ValueError("downtrend_multiplier must be between 0 and 1")
    downtrend = bool(state.get("downtrend", False))
    factor = float(downtrend_multiplier) if downtrend else 1.0
    return {symbol: float(value) * factor for symbol, value in weights.items() if value}


PORTFOLIO_OVERLAYS = {
    "per_coin_cap": per_coin_cap,
    "gross_target": gross_target,
    "ema_smooth": ema_smooth,
    "regime_throttle": regime_throttle,
}
