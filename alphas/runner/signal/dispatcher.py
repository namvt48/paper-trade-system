from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone


logger = logging.getLogger(__name__)


class SignalDispatcher:
    def __init__(self, redis_client, stream: str, lease_manager, max_seen: int = 4096,
                 metrics=None):
        self.redis_client = redis_client
        self.stream = stream
        self.lease_manager = lease_manager
        self.metrics = metrics
        self.max_seen = int(max_seen)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dedup_key = f"runner:dedup:{stream}"
        self._restore_seen()

    def _restore_seen(self) -> None:
        try:
            members = self.redis_client.lrange(self._dedup_key, 0, self.max_seen - 1)
            for m in reversed(members):
                sid = m.decode() if isinstance(m, bytes) else str(m)
                self._seen[sid] = None
        except Exception:
            pass

    async def dispatch(self, ctx, signal_type: str, **fields) -> str | None:
        if self.metrics is not None:
            self.metrics.inc_signal_dispatched()
        if not self.lease_manager.is_valid(ctx.alpha_id):
            ctx.state.lease_valid = False
            if self.metrics is not None:
                self.metrics.inc_signal_lease_dropped(ctx.alpha_id)
            logger.error(
                "[SIGNAL] Lease invalid, dropping signal type=%s alpha=%s",
                signal_type,
                ctx.alpha_id,
                extra={"alpha_id": ctx.alpha_id},
            )
            return None
        signal_id = self.signal_id(ctx.alpha_id, ctx.version, signal_type, fields)
        if signal_id in self._seen:
            if self.metrics is not None:
                self.metrics.inc_signal_dedup_skipped(ctx.alpha_id)
            logger.info(
                "[SIGNAL] Duplicate skipped type=%s alpha=%s symbol=%s tf=%s signal_id=%s",
                signal_type,
                ctx.alpha_id,
                fields.get("symbol", ""),
                fields.get("tf", ""),
                signal_id,
                extra={"alpha_id": ctx.alpha_id},
            )
            return None
        self._remember(signal_id)

        payload = {
            "type": signal_type,
            "alpha_id": ctx.alpha_id,
            "signal_id": signal_id,
            "timestamp": fields.pop("timestamp", datetime.now(timezone.utc).isoformat()),
        }
        for key, value in fields.items():
            if value is None:
                continue
            payload[key] = value if isinstance(value, str) else str(value)
        self.redis_client.xadd(self.stream, payload)
        if self.metrics is not None:
            self.metrics.inc_signal_published()
        logger.info(
            "[SIGNAL] Dispatched type=%s alpha=%s symbol=%s tf=%s signal_id=%s",
            signal_type,
            ctx.alpha_id,
            payload.get("symbol", ""),
            payload.get("tf", ""),
            signal_id,
            extra={"alpha_id": ctx.alpha_id},
        )
        return signal_id

    @staticmethod
    def signal_id(alpha_id: str, version: str, signal_type: str, fields: dict) -> str:
        logical = {
            "alpha_id": alpha_id,
            "version": version,
            "type": signal_type,
            "symbol": fields.get("symbol", ""),
            "tf": fields.get("tf", ""),
            "side": fields.get("side", ""),
            "position_id": fields.get("position_id", ""),
            "signal_candle_open_ms": fields.get("signal_candle_open_ms", ""),
            "reason": fields.get("reason", ""),
        }
        raw = json.dumps(logical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _remember(self, signal_id: str) -> None:
        self._seen[signal_id] = None
        self._seen.move_to_end(signal_id)
        while len(self._seen) > self.max_seen:
            self._seen.popitem(last=False)
        try:
            pipe = self.redis_client.pipeline(transaction=False)
            pipe.lpush(self._dedup_key, signal_id)
            pipe.ltrim(self._dedup_key, 0, self.max_seen - 1)
            pipe.execute()
        except Exception:
            pass
