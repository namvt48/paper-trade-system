from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timezone


class SignalDispatcher:
    def __init__(self, redis_client, stream: str, lease_manager, max_seen: int = 4096):
        self.redis_client = redis_client
        self.stream = stream
        self.lease_manager = lease_manager
        self.max_seen = int(max_seen)
        self._seen: OrderedDict[str, None] = OrderedDict()

    async def dispatch(self, ctx, signal_type: str, **fields) -> str | None:
        if not self.lease_manager.is_valid(ctx.alpha_id):
            ctx.state.lease_valid = False
            return None
        signal_id = self.signal_id(ctx.alpha_id, ctx.version, signal_type, fields)
        if signal_id in self._seen:
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

