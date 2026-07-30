from __future__ import annotations

import logging
from typing import Any

from runner.data_layer.snapshot import SnapshotReader

logger = logging.getLogger(__name__)


def fetch_closes(
    redis_client: Any, exchange: str, symbol: str, timeframe: str, bars: int
) -> list[float] | None:
    """Bounded, synchronous read of completed closes for a regime provider.

    Reuses the runner's own MDS snapshot cache (kline_snapshot_v2) instead of
    standing up a second market-data pipeline. Returns None (never raises) on
    missing/stale/malformed data -- regime state is optional shadow input
    (see cross_alpha/spec.py book_only design notes), so a bad read must
    degrade to "no regime signal", not break the PM cycle.

    Must be invoked via asyncio.to_thread from async callers: this issues a
    synchronous redis-py call against mds-redis, which is a bounded
    request/response read (not the long-lived pubsub connection) but can
    still block the caller's thread if mds-redis is unresponsive -- exactly
    the failure mode that silenced 5 daily alphas in the runner on
    2026-07-16. The caller is responsible for a socket timeout on
    redis_client (mirroring runner/main.py's mds_client construction).
    """
    reader = SnapshotReader(redis_client, exchange)
    try:
        candles = reader.load(symbol, timeframe, bars)
    except Exception:
        logger.exception("[regime] snapshot read failed for %s %s", symbol, timeframe)
        return None
    if not candles:
        return None
    closes: list[float] = []
    for candle in candles:
        try:
            closes.append(float(candle["close"]))
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "[regime] candle missing/invalid close for %s %s", symbol, timeframe
            )
            return None
    return closes
