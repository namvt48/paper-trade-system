from __future__ import annotations

import logging

from runner.data_layer.cache import SharedCandleCache


logger = logging.getLogger(__name__)

TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
    "1d": 86_400_000,
}

_TF_ORDER = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]


def _best_source_tf(cache: SharedCandleCache, target_tf: str, symbol: str) -> str | None:
    target_idx = _TF_ORDER.index(target_tf) if target_tf in _TF_ORDER else len(_TF_ORDER)
    for tf in reversed(_TF_ORDER[:target_idx]):
        if cache.get_bar_count(symbol, tf) > 0:
            return tf
    return None


def rollup_from_1m(
    cache: SharedCandleCache,
    target_tf: str,
    symbols: list[str],
) -> int:
    return rollup_to_tf(cache, target_tf, symbols)


def rollup_to_tf(
    cache: SharedCandleCache,
    target_tf: str,
    symbols: list[str],
) -> int:
    tf_ms = TF_MS.get(target_tf)
    if tf_ms is None:
        logger.warning("[ROLLUP] Unknown TF %s, skipping", target_tf)
        return 0

    total = 0
    for symbol in symbols:
        source_tf = _best_source_tf(cache, target_tf, symbol)
        if source_tf is None:
            continue

        times = cache.get_times(symbol, source_tf)
        if not times:
            continue
        opens = cache.get_opens(symbol, source_tf)
        highs = cache.get_highs(symbol, source_tf)
        lows = cache.get_lows(symbol, source_tf)
        closes = cache.get_closes(symbol, source_tf)
        volumes = cache.get_volumes(symbol, source_tf)

        source_tf_ms = TF_MS.get(source_tf, 60_000)
        expected_parts = tf_ms // source_tf_ms

        count = 0
        buckets: dict[int, list[int]] = {}
        for idx, open_time in enumerate(times):
            bucket_start = (int(open_time) // tf_ms) * tf_ms
            buckets.setdefault(bucket_start, []).append(idx)

        for bar_start in sorted(buckets):
            indexes = buckets[bar_start]
            expected_times = [bar_start + i * source_tf_ms for i in range(expected_parts)]
            actual_times = [int(times[i]) for i in indexes]
            if actual_times != expected_times:
                continue

            bar_end = bar_start + tf_ms
            first = indexes[0]
            bar_open = opens[first]
            bar_high = max(highs[i] for i in indexes)
            bar_low = min(lows[i] for i in indexes)
            bar_close = closes[indexes[-1]]
            bar_volume = sum(volumes[i] for i in indexes)

            candle = {
                "open_time": bar_start,
                "close_time": bar_end - 1,
                "open": bar_open,
                "high": bar_high,
                "low": bar_low,
                "close": bar_close,
                "volume": bar_volume,
            }
            cache.upsert_candle(symbol, target_tf, candle)
            count += 1

        total += count

    logger.debug("[ROLLUP] Built %d %s bars across %d symbols", total, target_tf, len(symbols))
    return total
