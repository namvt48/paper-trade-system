"""Capture immutable historical candles and funding from read-only MDS sources."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import pyarrow.parquet as pq

from .configuration import RecoveryAlphaConfig, load_symbols
from .domain import RecoveryPoint
from .storage import Workspace, write_json

MIN_SYMBOL_COVERAGE = 0.90
type MarketScalar = str | int | float | bool | None
type MarketRow = dict[str, MarketScalar]


class RedisListReader(Protocol):
    """Minimal read-only Redis contract required during capture."""

    def lrange(self, name: str, start: int, end: int) -> Sequence[str | bytes]: ...


class MarketCaptureError(Exception):
    """Required historical market input is missing or malformed."""


def capture_market_inputs(
    workspace: Workspace,
    configs: tuple[RecoveryAlphaConfig, ...],
    points: tuple[RecoveryPoint, ...],
    mds_cache: Path,
    redis_reader: RedisListReader,
) -> tuple[Path, ...]:
    """Freeze all native-timeframe, equity, and funding inputs into the run."""
    market_root = workspace.inputs / "market"
    market_root.mkdir(parents=True)
    captured: list[Path] = []
    equity_symbols: set[str] = set()
    due_configs: list[tuple[RecoveryAlphaConfig, RecoveryPoint, tuple[str, ...]]] = []
    candle_plans: dict[tuple[str, str, str], tuple[int, int]] = {}
    output_owners: dict[tuple[str, str], str] = {}
    funding_keys: set[tuple[str, str]] = set()
    equity_start_ms = min(int(point.candle_open_ms) for point in points) - 900_000
    for config in configs:
        symbols = load_symbols(config)
        equity_symbols.update(symbols)
        alpha_points = tuple(
            point for point in points if point.alpha_id == config.alpha_id
        )
        if not alpha_points:
            continue
        point = alpha_points[0]
        max_candle = max(int(item.candle_open_ms) for item in alpha_points)
        due_configs.append((config, point, symbols))
        for symbol in symbols:
            output_key = (point.timeframe, symbol)
            owner = output_owners.setdefault(output_key, config.exchange)
            if owner != config.exchange:
                raise MarketCaptureError(
                    f"market output collision for {point.timeframe}/{symbol}"
                )
            key = (config.exchange, point.timeframe, symbol)
            previous_candle, previous_warmup = candle_plans.get(key, (0, 0))
            candle_plans[key] = (
                max(max_candle, previous_candle),
                max(config.warmup_bars, previous_warmup),
            )
        spec = json.loads(config.spec_path.read_text(encoding="utf-8"))
        if bool(spec.get("needs_funding")):
            for symbol in symbols:
                funding_keys.add((config.exchange, symbol))
    availability: dict[tuple[str, str, str], int] = {}
    for key, (max_candle, warmup_bars) in sorted(candle_plans.items()):
        exchange, timeframe, symbol = key
        candles = _merged_candles(
            mds_cache,
            exchange,
            timeframe,
            symbol,
            redis_reader,
        )
        eligible = [row for row in candles if _row_int(row, "open_time") <= max_candle]
        availability[key] = len(eligible)
        tail = eligible[-(warmup_bars + 16) :]
        if tail:
            path = market_root / timeframe / f"{symbol}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, tail)
            captured.append(path)
    for config, point, symbols in due_configs:
        covered = sum(
            availability.get((config.exchange, point.timeframe, symbol), 0)
            >= config.warmup_bars
            for symbol in symbols
        )
        required = math.ceil(len(symbols) * MIN_SYMBOL_COVERAGE)
        if covered < required:
            raise MarketCaptureError(
                f"insufficient {point.timeframe} coverage for {config.alpha_id}: "
                f"{covered}/{len(symbols)}; required={required}"
            )
    for exchange, symbol in sorted(funding_keys):
        rows = _redis_rows(
            redis_reader,
            f"funding_snapshot:{exchange}:{symbol}",
            500,
        )
        path = market_root / "funding" / f"{symbol}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, rows)
        captured.append(path)
    for symbol in sorted(equity_symbols):
        candles = _merged_candles(mds_cache, "binance", "15m", symbol, redis_reader)
        candles = [
            row for row in candles if _row_int(row, "open_time") >= equity_start_ms
        ]
        path = market_root / "equity-15m" / f"{symbol}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, candles)
        captured.append(path)
    return tuple(sorted(set(captured)))


def _merged_candles(
    cache_root: Path,
    exchange: str,
    timeframe: str,
    symbol: str,
    redis_reader: RedisListReader,
) -> list[MarketRow]:
    """Overlay Redis corrections on Parquet history by candle open time."""
    rows = _parquet_rows(cache_root / exchange / timeframe / symbol)
    rows.extend(
        _redis_rows(
            redis_reader,
            f"kline_snapshot_v2:{exchange}:{timeframe}:{symbol}",
            100_000,
        )
    )
    by_time = {
        _row_int(row, "open_time", "time"): row
        for row in rows
        if _row_int(row, "open_time", "time") > 0
    }
    return [by_time[open_time] for open_time in sorted(by_time)]


def _parquet_rows(directory: Path) -> list[MarketRow]:
    """Read base then delta Parquet files using the runner's overwrite order."""
    if not directory.is_dir():
        return []
    files = sorted(
        directory.glob("*.parquet"),
        key=lambda path: (path.name != "base.parquet", path.name),
    )
    result: list[MarketRow] = []
    columns = ("open_time", "open", "high", "low", "close", "volume")
    for path in files:
        table = pq.read_table(path, columns=list(columns))
        values = table.to_pydict()
        result.extend(
            {column: values[column][index] for column in columns}
            for index in range(len(table))
        )
    return result


def _redis_rows(reader: RedisListReader, key: str, limit: int) -> list[MarketRow]:
    """Decode one read-only Redis list, rejecting malformed JSON at capture."""
    result: list[MarketRow] = []
    for raw in reader.lrange(key, 0, limit - 1):
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise MarketCaptureError(f"non-object row in Redis key {key}")
        row: MarketRow = {}
        for field, value in parsed.items():
            if not isinstance(field, str) or not isinstance(
                value, (str, int, float, bool, type(None))
            ):
                raise MarketCaptureError(f"non-scalar field in Redis key {key}")
            row[field] = value
        result.append(row)
    return result


def _row_int(row: MarketRow, key: str, fallback: str | None = None) -> int:
    """Read a required integer-like market field at the capture boundary."""
    value = row.get(key)
    if value is None and fallback is not None:
        value = row.get(fallback)
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise MarketCaptureError(f"invalid integer market field: {key}")
    return int(value)
