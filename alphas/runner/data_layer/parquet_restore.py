from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping

import pyarrow.parquet as pq

from runner.data_layer.cache import SharedCandleCache


logger = logging.getLogger(__name__)

SCHEMA_COLUMNS = (
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "confirmed",
)

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def read_parquet_candles(
    cache_dir: str,
    exchange: str,
    symbol: str,
    *,
    tf: str = "1m",
    tail_rows: int | None = None,
) -> list[dict]:
    symbol_dir = os.path.join(cache_dir, exchange, tf, symbol)
    if not os.path.isdir(symbol_dir):
        return []

    files: list[str] = []
    for fname in os.listdir(symbol_dir):
        if fname.endswith(".parquet"):
            files.append(fname)
    if not files:
        return []

    files.sort(key=lambda f: (0 if f == "base.parquet" else 1, f))

    all_candles: list[dict] = []
    for fname in files:
        fpath = os.path.join(symbol_dir, fname)
        try:
            table = pq.read_table(fpath, columns=list(SCHEMA_COLUMNS))
            row_count = len(table)
            if tail_rows is not None and tail_rows > 0 and row_count > int(tail_rows):
                table = table.slice(row_count - int(tail_rows), int(tail_rows))
                row_count = len(table)
            rows = table.to_pydict()
            for i in range(row_count):
                candle = {}
                for col in SCHEMA_COLUMNS:
                    if col in rows:
                        val = rows[col][i]
                        candle[col] = val.item() if hasattr(val, "item") else val
                all_candles.append(candle)
        except Exception as exc:
            logger.warning("[PARQUET-RESTORE] Failed to read %s: %s", fpath, exc)
            continue

    by_open_time: dict[int, dict] = {}
    for candle in all_candles:
        by_open_time[candle["open_time"]] = candle

    result = [by_open_time[t] for t in sorted(by_open_time)]
    if tail_rows is not None and tail_rows > 0:
        return result[-int(tail_rows) :]
    return result


def get_latest_open_time(
    cache_dir: str, exchange: str, symbol: str, *, tf: str = "1m"
) -> int | None:
    candles = read_parquet_candles(cache_dir, exchange, symbol, tf=tf)
    if not candles:
        return None
    return candles[-1]["open_time"]


def _detect_source_tf_ms(candles: list[dict]) -> int | None:
    if len(candles) < 2:
        return None
    times = []
    for c in candles[:10]:
        try:
            times.append(int(c["open_time"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(times) < 2:
        return None
    diffs = [
        times[i + 1] - times[i]
        for i in range(len(times) - 1)
        if times[i + 1] > times[i]
    ]
    if not diffs:
        return None
    min_diff = min(diffs)
    for tf_val in sorted(TF_MS.values()):
        if abs(min_diff - tf_val) < tf_val * 0.5:
            return tf_val
    return None


def _rollup_candles(candles: list[dict], target_tf: str) -> list[dict]:
    tf_ms = TF_MS.get(target_tf)
    if tf_ms is None:
        logger.warning("[PARQUET-RESTORE] Unknown TF %s, skipping rollup", target_tf)
        return []

    source_tf_ms = _detect_source_tf_ms(candles)
    if source_tf_ms is None:
        source_tf_ms = 60_000
    expected_parts = tf_ms // source_tf_ms
    buckets: dict[int, list[dict]] = {}
    for candle in candles:
        try:
            open_time = int(candle["open_time"])
        except (KeyError, TypeError, ValueError):
            continue
        bucket_start = (open_time // tf_ms) * tf_ms
        buckets.setdefault(bucket_start, []).append(candle)

    rolled: list[dict] = []
    for bar_start in sorted(buckets):
        bucket = sorted(buckets[bar_start], key=lambda c: int(c["open_time"]))
        actual_times = [int(c["open_time"]) for c in bucket]
        expected_times = [bar_start + i * source_tf_ms for i in range(expected_parts)]
        if actual_times != expected_times:
            continue
        rolled.append(
            {
                "open_time": bar_start,
                "close_time": bar_start + tf_ms - 1,
                "open": bucket[0]["open"],
                "high": max(float(c["high"]) for c in bucket),
                "low": min(float(c["low"]) for c in bucket),
                "close": bucket[-1]["close"],
                "volume": sum(float(c["volume"]) for c in bucket),
            }
        )
    return rolled


def _discover_available_tfs(cache_dir: str, exchange: str) -> list[str]:
    base = os.path.join(cache_dir, exchange)
    if not os.path.isdir(base):
        return []
    available = []
    for tf in ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]:
        if os.path.isdir(os.path.join(base, tf)):
            available.append(tf)
    return available


def _discover_symbols(
    cache_dir: str, exchange: str, available_tfs: list[str] | None = None
) -> list[str]:
    tfs = available_tfs or _discover_available_tfs(cache_dir, exchange)
    if not tfs:
        return []
    primary = tfs[0]
    primary_dir = os.path.join(cache_dir, exchange, primary)
    if not os.path.isdir(primary_dir):
        return []
    return sorted(
        d
        for d in os.listdir(primary_dir)
        if os.path.isdir(os.path.join(primary_dir, d))
    )


def restore_from_parquet(
    cache_dir: str,
    exchange: str,
    cache: SharedCandleCache,
    tfs_to_rollup: list[str] | None = None,
    symbols: Iterable[str] | None = None,
    tail_rows_by_symbol: Mapping[str, int] | None = None,
    clear_unrequired_1m_after_rollup: bool = False,
    requirements: dict[tuple[str, str], int] | None = None,
) -> int:
    available_tfs = _discover_available_tfs(cache_dir, exchange)
    if not available_tfs:
        logger.warning(
            "[PARQUET-RESTORE] No parquet cache found at %s/%s, falling back to MDS warmup",
            cache_dir,
            exchange,
        )
        return 0

    primary_tf = available_tfs[0]
    primary_dir = os.path.join(cache_dir, exchange, primary_tf)
    available_symbols = [
        d
        for d in os.listdir(primary_dir)
        if os.path.isdir(os.path.join(primary_dir, d))
    ]
    if symbols is None:
        restore_symbols = available_symbols
    else:
        requested = {str(symbol) for symbol in symbols}
        restore_symbols = [
            symbol for symbol in available_symbols if symbol in requested
        ]

    rollup_tfs = [tf for tf in (tfs_to_rollup or []) if tf != "1m"]
    needs_1m = (
        "1m" in available_tfs
        and any(tf in available_tfs for tf in ["1m"])
        or not rollup_tfs
    )
    rollup_only = bool(rollup_tfs and clear_unrequired_1m_after_rollup)

    # Directly-restorable TFs are the ones with their own parquet directory.
    # When 1m is also required (needs_1m) but the 1m parquet dir is empty (MDS
    # does not persist 1m), the old code only restored via the 1m -> rollup
    # path and restored ZERO candles for the larger TFs, forcing a slow MDS
    # warmup for every symbol. Always restore the larger TFs directly from
    # their own parquet; 1m is only used as a rollup source when it actually
    # has data.
    direct_tfs = [tf for tf in rollup_tfs if tf in available_tfs]
    fallback_tfs = [tf for tf in rollup_tfs if tf not in available_tfs]

    intermediate_tfs = []
    if not rollup_only and rollup_tfs:
        for tf in available_tfs:
            if (
                tf != "1m"
                and tf not in rollup_tfs
                and TF_MS.get(tf, 0) < max(TF_MS.get(rt, 0) for rt in rollup_tfs)
            ):
                intermediate_tfs.append(tf)

    total_restored = 0

    if direct_tfs:
        total_restored += _restore_direct_tfs(
            cache_dir,
            exchange,
            cache,
            restore_symbols,
            direct_tfs,
            tail_rows_by_symbol,
            requirements=requirements,
        )

    if fallback_tfs:
        total_restored += _restore_via_1m_rollup(
            cache_dir,
            exchange,
            cache,
            restore_symbols,
            fallback_tfs,
            tail_rows_by_symbol,
        )

    if intermediate_tfs:
        total_restored += _restore_direct_tfs(
            cache_dir,
            exchange,
            cache,
            restore_symbols,
            intermediate_tfs,
            tail_rows_by_symbol,
            requirements=requirements,
        )

    if not rollup_only:
        if "1m" in available_tfs:
            for symbol in sorted(restore_symbols):
                tail_rows = None
                if tail_rows_by_symbol is not None:
                    tail_rows = tail_rows_by_symbol.get(symbol)
                candles = read_parquet_candles(
                    cache_dir, exchange, symbol, tf="1m", tail_rows=tail_rows
                )
                before = cache.get_bar_count(symbol, "1m")
                for candle in candles:
                    cache.upsert_candle(symbol, "1m", candle)
                after = cache.get_bar_count(symbol, "1m")
                added = after - before
                total_restored += added
                if added > 0:
                    logger.debug(
                        "[PARQUET-RESTORE] %s 1m: %d candles restored", symbol, added
                    )
                if added > 0 and fallback_tfs:
                    from runner.data_layer.rollup import rollup_from_1m

                    for tf in fallback_tfs:
                        rollup_from_1m(cache, tf, [symbol])
                    if clear_unrequired_1m_after_rollup:
                        cache.trim_tf_to_requirements(
                            "1m", remove_unrequired=True, symbols=[symbol]
                        )
        else:
            if rollup_tfs:
                from runner.data_layer.rollup import rollup_to_tf

                for tf in rollup_tfs:
                    count = rollup_to_tf(cache, tf, restore_symbols)
                    total_restored += count
                    if count > 0:
                        logger.debug(
                            "[PARQUET-RESTORE] Rollup to %s: %d bars", tf, count
                        )

    logger.info(
        "[PARQUET-RESTORE] Restored %d candles across %d symbols (direct=%s fallback=%s intermediate=%s) from %s",
        total_restored,
        len(restore_symbols),
        ",".join(direct_tfs) or "-",
        ",".join(fallback_tfs) or "-",
        ",".join(intermediate_tfs) or "-",
        cache_dir,
    )
    return total_restored


def _restore_direct_tfs(
    cache_dir: str,
    exchange: str,
    cache: SharedCandleCache,
    restore_symbols: list[str],
    direct_tfs: list[str],
    tail_rows_by_symbol: Mapping[str, int] | None,
    requirements: dict[tuple[str, str], int] | None,
) -> int:
    total_restored = 0
    restore_set = set(restore_symbols)
    for tf in direct_tfs:
        tf_dir = os.path.join(cache_dir, exchange, tf)
        if not os.path.isdir(tf_dir):
            continue
        available = [
            d for d in os.listdir(tf_dir) if os.path.isdir(os.path.join(tf_dir, d))
        ]
        symbols_in_tf = {s for s in available if s in restore_set}

        for symbol in sorted(symbols_in_tf):
            tail_rows = None
            if requirements is not None:
                bars = requirements.get((symbol, tf), 0)
                if bars > 0:
                    tail_rows = bars + max(2, 10)
            elif tail_rows_by_symbol is not None:
                tf_minutes = TF_MS.get(tf, 60_000) // 60_000
                raw = tail_rows_by_symbol.get(symbol)
                if raw is not None and tf_minutes > 1:
                    tail_rows = max(1, raw // tf_minutes) + 10

            candles = read_parquet_candles(
                cache_dir, exchange, symbol, tf=tf, tail_rows=tail_rows
            )
            if not candles:
                continue
            before = cache.get_bar_count(symbol, tf)
            for candle in candles:
                cache.upsert_candle(symbol, tf, candle)
            added = cache.get_bar_count(symbol, tf) - before
            total_restored += added
            if added > 0:
                logger.debug(
                    "[PARQUET-RESTORE] %s %s direct: %d candles restored",
                    symbol,
                    tf,
                    added,
                )

    return total_restored


def _restore_via_1m_rollup(
    cache_dir: str,
    exchange: str,
    cache: SharedCandleCache,
    restore_symbols: list[str],
    fallback_tfs: list[str],
    tail_rows_by_symbol: Mapping[str, int] | None,
) -> int:
    total_restored = 0
    for symbol in sorted(restore_symbols):
        added_for_symbol = 0
        for tf in fallback_tfs:
            source = _find_rollup_source(cache_dir, exchange, symbol, tf)
            if source is None:
                continue
            tail_rows = None
            if tail_rows_by_symbol is not None:
                source_minutes = TF_MS.get(source, 60_000) // 60_000
                raw = tail_rows_by_symbol.get(symbol)
                if raw is not None and source_minutes > 1:
                    tail_rows = max(1, raw // source_minutes) + 10
            candles = read_parquet_candles(
                cache_dir, exchange, symbol, tf=source, tail_rows=tail_rows
            )
            if not candles:
                continue
            before = cache.get_bar_count(symbol, tf)
            for candle in _rollup_candles(candles, tf):
                cache.upsert_candle(symbol, tf, candle)
            added_for_symbol += cache.get_bar_count(symbol, tf) - before
        total_restored += added_for_symbol
        if added_for_symbol > 0:
            logger.debug(
                "[PARQUET-RESTORE] %s rollup: %d candles restored",
                symbol,
                added_for_symbol,
            )

    return total_restored


def _find_rollup_source(
    cache_dir: str,
    exchange: str,
    symbol: str,
    target_tf: str,
) -> str | None:
    target_ms = TF_MS.get(target_tf)
    if target_ms is None:
        return None
    for tf in reversed(["1m", "5m", "15m", "30m", "1h", "4h"]):
        tf_ms = TF_MS.get(tf)
        if tf_ms is None or tf_ms >= target_ms:
            continue
        tf_dir = os.path.join(cache_dir, exchange, tf, symbol)
        if os.path.isdir(tf_dir) and os.listdir(tf_dir):
            return tf
    return None
