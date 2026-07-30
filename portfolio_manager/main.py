from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import redis
import redis.asyncio as redis_async

from base import signal_push
from portfolio_manager.app.service import PortfolioService
from portfolio_manager.core.market_data import fetch_closes
from portfolio_manager.core.regime import btc_trend_state

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


async def _compute_regime_state(
    mds_client: Any, regime_cfg: dict[str, Any]
) -> dict[str, float | bool]:
    # Only "btc_trend" is implemented today (see core/regime.py); other
    # registry entries would need their own calling convention, so this is
    # intentionally special-cased rather than a generic dispatch over one
    # provider.
    if regime_cfg.get("provider") != "btc_trend":
        return {}
    symbol = str(regime_cfg.get("symbol", "BTCUSDT"))
    timeframe = str(regime_cfg.get("timeframe", "1h"))
    exchange = str(regime_cfg.get("exchange", "binance"))
    lookback = int(regime_cfg.get("lookback_bars", 24))
    threshold = float(regime_cfg.get("threshold", 0.0))
    closes = await asyncio.to_thread(
        fetch_closes, mds_client, exchange, symbol, timeframe, lookback + 2
    )
    if closes is None:
        logger.warning(
            "[pm-live] regime: no BTC closes available; cycle runs with no regime signal"
        )
        return {}
    return btc_trend_state(closes, lookback, threshold)


async def _safety_poll(
    service: PortfolioService,
    mds_client: Any,
    regime_cfg: dict[str, Any],
    poll_sec: float,
) -> None:
    while True:
        try:
            regime_state = await _compute_regime_state(mds_client, regime_cfg)
            result = service.run_cycle(regime_state=regime_state)
            logger.info(
                "[pm-live] safety cycle active=%s stale=%s published=%d execution=%s regime=%s",
                result["active_sleeves"],
                result["stale_sleeves"],
                result["published"],
                result["execution_enabled"],
                regime_state,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[pm-live] safety cycle failed; next poll will retry")
        await asyncio.sleep(poll_sec)


async def _event_loop(
    service: PortfolioService,
    redis_client,
    mds_client: Any,
    regime_cfg: dict[str, Any],
    debounce_sec: float,
) -> None:
    pubsub = redis_client.pubsub()
    channels = [f"book:updated:{sleeve['id']}" for sleeve in service.config["sleeves"]]
    await pubsub.subscribe(*channels)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if message:
                await asyncio.sleep(debounce_sec)
                try:
                    regime_state = await _compute_regime_state(mds_client, regime_cfg)
                    result = service.run_cycle(regime_state=regime_state)
                    logger.info(
                        "[pm-live] event cycle active=%s stale=%s published=%d",
                        result["active_sleeves"],
                        result["stale_sleeves"],
                        result["published"],
                    )
                except Exception:
                    logger.exception(
                        "[pm-live] event cycle failed; safety poll remains active"
                    )
            await asyncio.sleep(0.05)
    finally:
        await pubsub.close()
        await redis_client.aclose()


async def main() -> None:
    config_path = Path(
        os.getenv(
            "PORTFOLIO_CONFIG",
            Path(__file__).parent / "config" / "portfolio.json",
        )
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    signal_push.init(redis_url, str(config.get("signal_stream", "paper-signals")))
    redis_client = redis.from_url(redis_url, decode_responses=True)
    service = PortfolioService(config, redis_client)

    # mds_client is a bounded request/response reader (regime candles only),
    # never a long-lived subscription -- it must have a socket timeout so an
    # unresponsive mds-redis degrades the regime signal to "unknown" instead
    # of blocking a cycle forever (2026-07-16 incident: an unbounded MDS read
    # silenced 5 daily alphas in the runner).
    mds_socket_timeout = float(os.getenv("MDS_REDIS_SOCKET_TIMEOUT_SEC", "10.0"))
    mds_client = redis.from_url(
        os.getenv("MDS_REDIS_URL") or redis_url,
        decode_responses=True,
        socket_timeout=mds_socket_timeout,
        socket_connect_timeout=mds_socket_timeout,
    )
    regime_cfg = dict(config.get("regime", {}))

    poll_sec = float(config["trigger"].get("safety_poll_sec", 60))
    async_redis = redis_async.from_url(redis_url, decode_responses=True)
    await asyncio.gather(
        _safety_poll(service, mds_client, regime_cfg, poll_sec),
        _event_loop(
            service,
            async_redis,
            mds_client,
            regime_cfg,
            float(config["trigger"].get("debounce_sec", 5)),
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
