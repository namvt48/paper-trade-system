from __future__ import annotations

import asyncio


def adverse_price(order_side: str, initial: float, delayed: float) -> float:
    return max(initial, delayed) if order_side.upper() == "BUY" else min(initial, delayed)


async def delayed_adverse_quote(query, *, order_side: str, initial_price: float,
                                latency_ms: float, sleeper=asyncio.sleep):
    await sleeper(latency_ms / 1000.0)
    delayed = await query()
    if delayed is None:
        return initial_price, None
    return adverse_price(order_side, initial_price, delayed), delayed
