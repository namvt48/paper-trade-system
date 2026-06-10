from __future__ import annotations

import json
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass

from app.fill import resolve_fill_price
from app.execution_model import adverse_price

logger = logging.getLogger(__name__)


@dataclass
class FillResolution:
    final_price: float
    order_side: str
    initial_price: float
    delayed_price: float | None = None
    initial_source: str = "fixed_pct"
    delayed_source: str | None = None
    initial_book_state: str | None = None
    delayed_book_state: str | None = None
    initial_snapshot_timestamp: str | None = None
    delayed_snapshot_timestamp: str | None = None
    initial_snapshot_age_ms: float | None = None
    delayed_snapshot_age_ms: float | None = None
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    model_latency_ms: float = 0.0
    adverse_movement_bps: float = 0.0
    book_slippage_bps: float | None = None
    fallback_reason: str | None = None
    pre_subscribe_outcome: str | None = None

    def __float__(self) -> float:
        return self.final_price

    def __eq__(self, other) -> bool:
        if isinstance(other, (int, float)):
            return self.final_price == float(other)
        if isinstance(other, FillResolution):
            return self.metadata() == other.metadata()
        return NotImplemented

    def metadata(self) -> dict:
        return asdict(self)


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
        _, raw = item
        try:
            resp = json.loads(raw)
            if not isinstance(resp, dict):
                raise ValueError("response is not an object")
            if resp.get("request_id") not in (None, rid):
                raise ValueError("response request_id mismatch")
            for key in ("avg_exec_price", "filled_qty", "requested_qty"):
                value = float(resp.get(key, 0.0))
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"invalid {key}")
        except Exception as exc:
            logger.warning("[SLIP-RPC] invalid response for %s: %s", symbol, exc)
            self._record_failure()
            return None
        self._record_success()
        return resp


class FillService:
    """Resolves a fill price: RPC walk if available, else fixed-pct fallback."""

    def __init__(self, client: SlippageClient, slippage_pct: float, timeout: float = 0.2,
                 supported_exchanges: set[str] | None = None,
                 latency_model_enabled: bool = False, latency_ms: float = 50.0,
                 min_adverse_bps: float = 0.0, second_quote_timeout: float = 0.2,
                 sleeper=None) -> None:
        self._client = client
        self._slippage_pct = slippage_pct
        self._timeout = timeout
        self._supported_exchanges = {
            exchange.lower() for exchange in (supported_exchanges or {"binance"})
        }
        self._latency_model_enabled = latency_model_enabled
        self._latency_ms = latency_ms
        self._min_adverse_bps = min_adverse_bps
        self._second_quote_timeout = second_quote_timeout
        self._sleeper = sleeper

    async def resolve(self, exchange: str, symbol: str, position_side: str, qty: float,
                      ref_price: float, is_close: bool, request_id: str | None = None,
                      ref_is_executable: bool = False) -> FillResolution:
        order_side = order_side_for(position_side, is_close)
        resp = None
        supported = exchange.lower() in self._supported_exchanges
        if supported:
            resp = await self._client.query(
                exchange.lower(), symbol, order_side, qty,
                fallback_pct=self._slippage_pct, timeout=self._timeout, request_id=request_id,
            )
        initial = resolve_fill_price(resp, ref_price, position_side, is_close, self._slippage_pct,
                                     ref_is_executable=ref_is_executable)
        source = str(resp.get("source", "unknown")) if resp else "fixed_pct"
        fallback_reason = None
        if not supported:
            source, fallback_reason = "fixed_pct", "unsupported_exchange"
        elif resp is None:
            source, fallback_reason = ("executable_ref" if ref_is_executable else "fixed_pct"), "rpc_unavailable"
        elif resp.get("fallback_used"):
            source, fallback_reason = "fixed_pct", str(resp.get("fallback_reason") or "mds_fallback")
        snapshot_ts = float(resp.get("snapshot_ts", 0.0)) if resp else 0.0
        snapshot_age_ms = max(0.0, time.time() * 1000.0 - snapshot_ts) if snapshot_ts > 0 else None
        resolution = FillResolution(
            final_price=initial, order_side=order_side, initial_price=initial,
            initial_source=source, requested_qty=qty,
            filled_qty=float(resp.get("filled_qty", 0.0)) if resp else 0.0,
            fallback_reason=fallback_reason,
            initial_book_state=resp.get("book_state") if resp else None,
            initial_snapshot_timestamp=str(int(snapshot_ts)) if snapshot_ts > 0 else None,
            initial_snapshot_age_ms=snapshot_age_ms,
        )
        if resp is not None and not resp.get("fallback_used"):
            try:
                resolution.book_slippage_bps = float(resp["slippage_bps"])
            except (KeyError, TypeError, ValueError):
                resolution.book_slippage_bps = None
        if not self._latency_model_enabled or resp is None or resp.get("fallback_used"):
            return resolution

        import asyncio
        sleeper = self._sleeper or asyncio.sleep
        await sleeper(self._latency_ms / 1000.0)
        delayed_resp = await self._client.query(
            exchange.lower(), symbol, order_side, qty, fallback_pct=self._slippage_pct,
            timeout=self._second_quote_timeout,
        )
        if delayed_resp is not None and not delayed_resp.get("fallback_used"):
            delayed = resolve_fill_price(
                delayed_resp, ref_price, position_side, is_close, self._slippage_pct,
                ref_is_executable=ref_is_executable,
            )
            resolution.delayed_price = delayed
            resolution.delayed_source = str(delayed_resp.get("source", "unknown"))
            resolution.delayed_book_state = delayed_resp.get("book_state")
            delayed_snapshot_ts = float(delayed_resp.get("snapshot_ts", 0.0))
            resolution.delayed_snapshot_timestamp = (
                str(int(delayed_snapshot_ts)) if delayed_snapshot_ts > 0 else None
            )
            resolution.delayed_snapshot_age_ms = (
                max(0.0, time.time() * 1000.0 - delayed_snapshot_ts)
                if delayed_snapshot_ts > 0 else None
            )
            resolution.final_price = adverse_price(order_side, initial, delayed)
        else:
            resolution.delayed_source = "failed"
            resolution.fallback_reason = "delayed_quote_failed"
        floor = initial * self._min_adverse_bps / 10000.0
        if floor > 0:
            floor_price = initial + floor if order_side == "BUY" else initial - floor
            resolution.final_price = adverse_price(order_side, resolution.final_price, floor_price)
        resolution.model_latency_ms = self._latency_ms
        if initial:
            direction = 1 if order_side == "BUY" else -1
            resolution.adverse_movement_bps = max(
                0.0, direction * (resolution.final_price - initial) / initial * 10000.0
            )
        return resolution
