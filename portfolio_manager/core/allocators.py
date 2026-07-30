from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def fixed_allocator(sleeves: Sequence[str], configured: Mapping[str, float]) -> dict[str, float]:
    """Return fixed nominal sleeve allocations; dynamic allocators are not implicit."""
    unknown = set(configured) - set(sleeves)
    if unknown:
        raise ValueError(f"allocation contains unknown sleeves: {sorted(unknown)}")
    result = {sleeve: float(configured.get(sleeve, 0.0)) for sleeve in sleeves}
    if any(not math.isfinite(value) or value < 0 for value in result.values()):
        raise ValueError("allocation weights must be finite and non-negative")
    total = sum(result.values())
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"fixed allocation must sum to 1.0, got {total}")
    return result


def inverse_vol_allocator(volatility: Mapping[str, float]) -> dict[str, float]:
    """Optional pure allocator; PM config keeps it disabled in Phase 1."""
    inverse = {sleeve: 1.0 / float(vol) for sleeve, vol in volatility.items() if float(vol) > 0}
    total = sum(inverse.values())
    return {sleeve: value / total for sleeve, value in inverse.items()} if total else {}


ALLOCATORS = {
    "fixed": fixed_allocator,
    "inverse_vol": inverse_vol_allocator,
}
