"""Song Than — zone-based mean-reversion strategy math.

Pure functions ported verbatim from docs/alphas/song_than.md. No I/O, no state —
the engine wires these into the BaseEngine event loop.

Pipeline:  OHLCV 15m  →  Swing Points  →  Trailing Levels  →  Zones  →  Signals
"""
from __future__ import annotations

import math
from typing import Any, Optional

NaN = float("nan")


# ── Section 2.1 — Swing Points ────────────────────────────────────────────────


def compute_swing_points(
    high: list[float],
    low: list[float],
    L: int = 50,
) -> tuple[list[float], list[float]]:
    """LuxAlgo SMC pivot detector.

    For each bar ``i >= L`` the pivot candidate is ``p = i - L``. The window
    ``(p+1 .. i)`` (L bars) is compared against ``high[p]`` / ``low[p]``.

    A swing is only marked when the leg *flips* (high→bearish, low→bullish).
    If both fire the same bar, ``is_pivot_high`` wins (spec note).
    """
    n = len(high)
    swing_high = [NaN] * n
    swing_low = [NaN] * n
    leg = 0  # 0 = Bearish, 1 = Bullish (starts Bearish)

    for i in range(L, n):
        p = i - L
        window_max_high = max(high[p + 1 : i + 1])
        window_min_low = min(low[p + 1 : i + 1])

        is_pivot_high = high[p] > window_max_high
        is_pivot_low = low[p] < window_min_low

        prev_leg = leg
        if is_pivot_high:
            leg = 0
        elif is_pivot_low:
            leg = 1

        if leg != prev_leg:
            if leg == 0:
                swing_high[p] = high[p]
            else:
                swing_low[p] = low[p]

    return swing_high, swing_low


# ── Section 2.2 — Trailing Levels ─────────────────────────────────────────────


def compute_trailing_levels(
    swing_high: list[float],
    swing_low: list[float],
    high: list[float],
    low: list[float],
    L: int = 50,
) -> tuple[list[float], list[float]]:
    """trail_up / trail_dn arrays.

    Per-bar order: (1) inspect swing at ``p = i - L`` → (2) reset if leg flipped
    → (3) expand with current bar (trail_up only rises, trail_dn only falls).
    """
    n = len(high)
    trail_up = [NaN] * n
    trail_dn = [NaN] * n

    leg = 0
    cur_up = -math.inf
    cur_dn = math.inf

    for i in range(n):
        if i >= L:
            p = i - L
            if not math.isnan(swing_high[p]) and leg != 0:
                cur_up = swing_high[p]
                leg = 0
            if not math.isnan(swing_low[p]) and leg != 1:
                cur_dn = swing_low[p]
                leg = 1

        cur_up = max(cur_up, high[i])
        cur_dn = min(cur_dn, low[i])

        trail_up[i] = cur_up
        trail_dn[i] = cur_dn

    return trail_up, trail_dn


# ── Section 2.3 — Zones ───────────────────────────────────────────────────────


def compute_zones(
    trail_up: float,
    trail_dn: float,
) -> Optional[tuple[float, float, float, float]]:
    """Return ``(green_low, green_high, red_low, red_high)`` or ``None`` if invalid.

    Red zone   = top 5%    of the range (resistance / premium).
    Green zone = bottom 5% of the range (support / discount).
    """
    if not (trail_up > trail_dn):
        return None
    red_high = trail_up
    red_low = trail_up * 0.95 + trail_dn * 0.05
    green_high = trail_dn * 0.95 + trail_up * 0.05
    green_low = trail_dn
    return green_low, green_high, red_low, red_high


# ── Section 3 — Entry signals ─────────────────────────────────────────────────


def get_entry_signals(
    bar_low: float,
    bar_high: float,
    green_high: float,
    red_low: float,
) -> list[dict[str, Any]]:
    """Signals emitted *after* a bar closes.

    A LONG limit rests at ``green_high``; a SHORT limit rests at ``red_low``.
    Both can fire the same bar — caller resolves the conflict.
    """
    signals: list[dict[str, Any]] = []
    if bar_low <= green_high:
        signals.append({"side": "LONG", "limit_price": green_high})
    if bar_high >= red_low:
        signals.append({"side": "SHORT", "limit_price": red_low})
    return signals


# ── Section 4.2 — Trailing milestones ─────────────────────────────────────────


def get_trailing_milestone(
    entry_price: float,
    side: str,
    bar_high: float,
    bar_low: float,
    m1_pct: float,
    m2_pct: float,
) -> int:
    """0, 1, or 2 — highest milestone reached this bar."""
    if side == "LONG":
        pnl_pct = (bar_high - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - bar_low) / entry_price
    if pnl_pct >= m2_pct:
        return 2
    if pnl_pct >= m1_pct:
        return 1
    return 0


def update_trailing_sl(
    entry_price: float,
    side: str,
    milestone: int,
    m1_sl_pct: float,
    m2_sl_pct: float,
) -> Optional[float]:
    """New SL price for the given milestone, or ``None`` if milestone 0."""
    if milestone >= 2:
        sl_offset = m2_sl_pct
    elif milestone >= 1:
        sl_offset = m1_sl_pct
    else:
        return None
    if side == "LONG":
        return entry_price * (1 + sl_offset)
    return entry_price * (1 - sl_offset)
