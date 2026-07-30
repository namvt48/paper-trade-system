from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from .allocators import fixed_allocator
from .book import TargetBook
from .overlays import ema_smooth, gross_target, per_coin_cap, regime_throttle


@dataclass(frozen=True)
class BlendResult:
    baseline: dict[str, float]
    candidate: dict[str, float]
    active_sleeves: tuple[str, ...]
    stale_sleeves: tuple[str, ...]


def blend_books(
    books: Mapping[str, TargetBook | None],
    allocations: Mapping[str, float],
    max_staleness_sec: Mapping[str, float],
    *,
    now: float | None = None,
) -> tuple[dict[str, float], tuple[str, ...], tuple[str, ...]]:
    sleeve_ids = tuple(allocations)
    fixed = fixed_allocator(sleeve_ids, allocations)
    output: dict[str, float] = {}
    active: list[str] = []
    stale: list[str] = []
    for sleeve_id, allocation in fixed.items():
        book = books.get(sleeve_id)
        if book is None or book.is_stale(max_staleness_sec[sleeve_id], now):
            stale.append(sleeve_id)
            continue
        active.append(sleeve_id)
        for symbol, weight in book.weights.items():
            output[symbol] = output.get(symbol, 0.0) + allocation * float(weight)
    return output, tuple(active), tuple(stale)


def build_blend_outputs(
    books: Mapping[str, TargetBook | None],
    allocations: Mapping[str, float],
    max_staleness_sec: Mapping[str, float],
    *,
    cap: float,
    gross: float,
    regime_state: Mapping[str, float | bool] | None = None,
    downtrend_multiplier: float = 1.0,
    previous: Mapping[str, float] | None = None,
    ema_span: int | None = None,
    now: float | None = None,
) -> BlendResult:
    blended, active, stale = blend_books(books, allocations, max_staleness_sec, now=now)
    baseline = gross_target(per_coin_cap(blended, cap), gross)
    # Never use the previous target when a source book is stale: doing so would
    # silently reintroduce the very weight the staleness gate removed.
    if ema_span is not None and baseline and not stale:
        baseline = ema_smooth(baseline, previous, ema_span)
    candidate = dict(baseline)
    if regime_state is not None:
        # Throttle is intentionally after gross targeting so a downtrend can
        # reduce exposure instead of being immediately re-scaled back to 1.0.
        candidate = regime_throttle(candidate, regime_state, downtrend_multiplier=downtrend_multiplier)
    return BlendResult(baseline=baseline, candidate=candidate, active_sleeves=active, stale_sleeves=stale)
