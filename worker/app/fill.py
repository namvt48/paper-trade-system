from __future__ import annotations

_EPS = 1e-12


def fixed_pct_fill(price: float, position_side: str, slippage_pct: float, is_close: bool) -> float:
    """Fixed-percentage slippage model (the legacy fallback).

    slippage_pct is in per-mille tenths as used today: slip = price * (slippage_pct / 1000).
    """
    slip = price * (slippage_pct / 1000.0)
    if position_side.upper() == "LONG":
        return (price - slip) if is_close else (price + slip)
    return (price + slip) if is_close else (price - slip)


def resolve_fill_price(
    resp: dict | None,
    ref_price: float,
    position_side: str,
    is_close: bool,
    slippage_pct: float,
) -> float:
    """Turn an MDS slippage RPC response into a fill price.

    Falls back to fixed-pct when the RPC is unavailable/fallback; blends the filled
    portion (book avg) with fixed-pct on any unfilled remainder.
    """
    if resp is None or resp.get("fallback_used"):
        return fixed_pct_fill(ref_price, position_side, slippage_pct, is_close)
    filled = float(resp.get("filled_qty", 0.0))
    requested = float(resp.get("requested_qty", 0.0))
    avg = float(resp.get("avg_exec_price", 0.0))
    if filled <= _EPS or avg <= 0.0:
        return fixed_pct_fill(ref_price, position_side, slippage_pct, is_close)
    if filled >= requested - _EPS:
        return avg
    remainder = requested - filled
    fixed_price = fixed_pct_fill(ref_price, position_side, slippage_pct, is_close)
    return (filled * avg + remainder * fixed_price) / requested
