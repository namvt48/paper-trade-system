from __future__ import annotations

import json
import logging
import time
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
    """Async client for the MDS slippage RPC: LPUSH request, BLPOP response.

    A lightweight circuit breaker prevents the serial signal loop from paying the full
    BLPOP timeout on every request while MDS is down: after ``failure_threshold``
    consecutive timeouts/errors the breaker opens for ``cooldown_sec``, during which
    ``query`` returns ``None`` immediately (caller falls back to fixed-pct) and no request
    is enqueued. The request list is also capped (LTRIM) so a backlog can't grow unbounded.
    """

    def __init__(self, redis_client, failure_threshold: int = 5, cooldown_sec: float = 10.0,
                 max_req_backlog: int = 1000, clock=time.monotonic) -> None:
        self._redis = redis_client
        self._failure_threshold = failure_threshold
        self._cooldown_sec = cooldown_sec
        self._max_req_backlog = max_req_backlog
        self._clock = clock
        self._failures = 0
        self._breaker_open_until: float | None = None

    def _breaker_open(self) -> bool:
        if self._breaker_open_until is None:
            return False
        if self._clock() >= self._breaker_open_until:
            self._breaker_open_until = None  # cooldown elapsed -> allow one probe
            return False
        return True

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._breaker_open_until = self._clock() + self._cooldown_sec

    def _record_success(self) -> None:
        self._failures = 0
        self._breaker_open_until = None

    async def query(self, exchange: str, symbol: str, side: str, qty: float,
                    fallback_pct: float = 0.0, timeout: float = 0.2,
                    request_id: str | None = None) -> dict | None:
        if self._breaker_open():
            return None  # MDS presumed down; skip the RPC, caller uses fixed-pct
        rid = request_id or uuid.uuid4().hex
        req = {
            "request_id": rid,
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "fallback_pct": fallback_pct,
        }
        req_key = f"orderbook:slip:req:{exchange}"
        resp_key = f"orderbook:slip:resp:{rid}"
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.lpush(req_key, json.dumps(req))
            pipe.ltrim(req_key, 0, self._max_req_backlog - 1)  # cap backlog (newest kept)
            await pipe.execute()
            item = await self._redis.blpop([resp_key], timeout=timeout)
        except Exception as exc:
            logger.warning("[SLIP-RPC] query failed for %s: %s", symbol, exc)
            self._record_failure()
            return None
        if not item:
            self._record_failure()
            return None
        self._record_success()
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
