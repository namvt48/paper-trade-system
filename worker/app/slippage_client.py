from __future__ import annotations

import json
import logging
import uuid

from app.fill import resolve_fill_price

logger = logging.getLogger(__name__)


def order_side_for(position_side: str, is_close: bool) -> str:
    """Map a position side + open/close to the order side the book is walked on.

    Open LONG -> BUY (consume asks); Open SHORT -> SELL (consume bids).
    Closing flips the side.
    """
    is_long = position_side.upper() == "LONG"
    if is_close:
        is_long = not is_long
    return "BUY" if is_long else "SELL"


class SlippageClient:
    """Async client for the MDS slippage RPC: LPUSH request, BLPOP response."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def query(self, exchange: str, symbol: str, side: str, qty: float,
                    fallback_pct: float = 0.0, timeout: float = 0.2,
                    request_id: str | None = None) -> dict | None:
        rid = request_id or uuid.uuid4().hex
        req = {
            "request_id": rid,
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "fallback_pct": fallback_pct,
        }
        resp_key = f"orderbook:slip:resp:{rid}"
        try:
            await self._redis.lpush(f"orderbook:slip:req:{exchange}", json.dumps(req))
            item = await self._redis.blpop([resp_key], timeout=timeout)
        except Exception as exc:
            logger.warning("[SLIP-RPC] query failed for %s: %s", symbol, exc)
            return None
        if not item:
            return None
        _, raw = item
        try:
            return json.loads(raw)
        except Exception:
            return None


class FillService:
    """Resolves a fill price: RPC walk if available, else fixed-pct fallback."""

    def __init__(self, client: SlippageClient, slippage_pct: float, timeout: float = 0.2) -> None:
        self._client = client
        self._slippage_pct = slippage_pct
        self._timeout = timeout

    async def resolve(self, exchange: str, symbol: str, position_side: str, qty: float,
                      ref_price: float, is_close: bool, request_id: str | None = None,
                      ref_is_executable: bool = False) -> float:
        order_side = order_side_for(position_side, is_close)
        resp = await self._client.query(
            exchange, symbol, order_side, qty,
            fallback_pct=self._slippage_pct, timeout=self._timeout, request_id=request_id,
        )
        return resolve_fill_price(resp, ref_price, position_side, is_close, self._slippage_pct,
                                  ref_is_executable=ref_is_executable)
