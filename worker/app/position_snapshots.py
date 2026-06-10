from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class PositionSnapshotPublisher:
    def __init__(self, db, redis_client, interval_sec: float = 5.0) -> None:
        self.db = db
        self.redis = redis_client
        self.interval_sec = interval_sec

    @staticmethod
    def _position(row: dict) -> dict:
        result = dict(row)
        raw = result.get("metadata")
        try:
            result["metadata"] = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            result["metadata"] = {"legacy_raw": raw}
        return result

    async def publish_alpha(self, alpha_id: str) -> dict:
        revision = await self.redis.incr(f"paper:positions:revision:{alpha_id}")
        positions = await self.db.get_all_open_positions(alpha_id)
        payload = {
            "alpha_id": alpha_id,
            "revision": int(revision),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "positions": [self._position(row) for row in positions],
        }
        await self.redis.set(
            f"paper:positions:snapshot:{alpha_id}",
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
        return payload

    async def publish_all(self) -> None:
        alpha_ids = {row["alpha_id"] for row in await self.db.get_all_alphas()}
        alpha_ids.update(
            row["alpha_id"] for row in await self.db.get_all_open_positions()
        )
        for alpha_id in sorted(alpha_ids):
            try:
                await self.publish_alpha(alpha_id)
            except Exception:
                logger.exception("[POSITION-SNAPSHOT] publish failed alpha=%s", alpha_id)

    async def publish_after_commit(self, alpha_id: str) -> None:
        try:
            await self.publish_alpha(alpha_id)
        except Exception:
            logger.exception("[POSITION-SNAPSHOT] post-commit publish failed alpha=%s", alpha_id)

    async def run(self) -> None:
        while True:
            try:
                await self.publish_all()
                await asyncio.sleep(self.interval_sec)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[POSITION-SNAPSHOT] repair loop failed")
                await asyncio.sleep(self.interval_sec)
