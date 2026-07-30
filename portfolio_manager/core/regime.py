from __future__ import annotations

from collections.abc import Sequence


def btc_trend_state(closes: Sequence[float], lookback: int, threshold: float = 0.0) -> dict[str, float | bool]:
    """Causal BTC trend state from completed closes only."""
    if lookback < 1:
        raise ValueError("lookback must be positive")
    if len(closes) <= lookback:
        return {"ready": False, "downtrend": False, "return": 0.0}
    start = float(closes[-lookback - 1])
    end = float(closes[-1])
    if start <= 0:
        raise ValueError("BTC close must be positive")
    trend_return = end / start - 1.0
    return {
        "ready": True,
        "downtrend": trend_return <= float(threshold),
        "return": trend_return,
    }


REGIME_PROVIDERS = {"btc_trend": btc_trend_state}
