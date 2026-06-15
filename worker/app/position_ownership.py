from __future__ import annotations

import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)


class PositionOwnershipMonitor:
    def __init__(self, db, paper_redis, mds_redis, grace_sec: float = 30.0,
                 interval_sec: float = 5.0, clock=time.monotonic,
                 summary_interval_sec: float = 300.0) -> None:
        self.db = db
        self.paper_redis = paper_redis
        self.mds_redis = mds_redis
        self.grace_sec = grace_sec
        self.interval_sec = interval_sec
        self.clock = clock
        self.summary_interval_sec = summary_interval_sec
        self._mismatch_since: float | None = None
        self._last_log_signature: str | None = None
        self._last_log_at: float | None = None
        self.last_report = {"healthy": True, "mismatch_count": 0, "details": []}

    async def check(self) -> dict:
        positions = await self.db.get_all_open_positions()
        grouped: dict[str, list[dict]] = {}
        for pos in positions:
            grouped.setdefault(pos["alpha_id"], []).append(pos)

        details = []
        for alpha_id, rows in sorted(grouped.items()):
            expected_ids = {row["position_id"] for row in rows}
            expected_symbols = {row["symbol"] for row in rows}
            paper_redis_error = None
            try:
                raw = await self.paper_redis.get(f"paper:alpha-runtime:{alpha_id}")
            except Exception as exc:
                raw = None
                paper_redis_error = str(exc)
            try:
                heartbeat = json.loads(raw) if raw else {}
            except (TypeError, json.JSONDecodeError):
                heartbeat = {}
            managed = set(heartbeat.get("managed_position_ids", []))
            desired = set(heartbeat.get("desired_price_alert_symbols", []))
            runtime_not_live = heartbeat.get("runtime_state") != "LIVE"
            actual: set[str] = set()
            redis_error = None
            try:
                for exchange in {str(row.get("exchange") or "binance").lower() for row in rows}:
                    actual.update(await self.mds_redis.smembers(
                        f"price_alert:subscriptions:{exchange}:{alpha_id}"
                    ))
            except Exception as exc:
                redis_error = str(exc)
            missing_ids = sorted(expected_ids - managed)
            extra_ids = sorted(managed - expected_ids)
            missing_subscriptions = sorted(expected_symbols - desired)
            missing_actual_subscriptions = sorted(expected_symbols - actual)
            if (missing_ids or extra_ids or missing_subscriptions or missing_actual_subscriptions
                    or redis_error or paper_redis_error or runtime_not_live):
                details.append({
                    "alpha_id": alpha_id,
                    "missing_ids": missing_ids,
                    "extra_ids": extra_ids,
                    "missing_subscriptions": missing_subscriptions,
                    "missing_actual_subscriptions": missing_actual_subscriptions,
                    "mds_redis_error": redis_error,
                    "paper_redis_error": paper_redis_error,
                    "runtime_not_live": runtime_not_live,
                })

        now = self.clock()
        if details:
            self._mismatch_since = self._mismatch_since or now
        else:
            self._mismatch_since = None
        degraded = bool(details) and now - self._mismatch_since >= self.grace_sec
        self.last_report = {
            "healthy": not degraded,
            "mismatch_count": len(details),
            "details": details,
        }
        if details:
            signature = json.dumps(details, sort_keys=True)
            if (
                signature != self._last_log_signature
                or self._last_log_at is None
                or now - self._last_log_at >= self.summary_interval_sec
            ):
                logger.warning("[POSITION-OWNERSHIP] %s", json.dumps(self.last_report, sort_keys=True))
                self._last_log_signature = signature
                self._last_log_at = now
        elif self._last_log_signature is not None:
            logger.info("[POSITION-OWNERSHIP] recovered")
            self._last_log_signature = None
            self._last_log_at = now
        return self.last_report

    async def run(self) -> None:
        while True:
            try:
                await self.check()
                await asyncio.sleep(self.interval_sec)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[POSITION-OWNERSHIP] monitor failed")
                self.last_report = {
                    "healthy": False, "mismatch_count": 1,
                    "details": [{"monitor_error": True}],
                }
                await asyncio.sleep(self.interval_sec)
