from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class CandleSnapshot:
    opens: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    closes: tuple[float, ...]
    volumes: tuple[float, ...]
    times: tuple[int, ...]


@dataclass(frozen=True)
class CacheKeyStats:
    symbol: str
    tf: str
    loaded_bars: int
    warmup_bars: int
    retain_bars: int
    trim_count: int


@dataclass(frozen=True)
class GapReport:
    symbol: str
    tf: str
    total_bars: int
    gap_count: int
    missing_ranges: tuple[tuple[int, int], ...]
    is_clean: bool


@dataclass(frozen=True)
class CandleArrayView:
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray
    times: np.ndarray


class NumpyCandleBuffer:
    def __init__(self, capacity: int = 1):
        self.capacity = max(1, int(capacity))
        self.start = 0
        self.size = 0
        self.opens = np.empty(self.capacity, dtype=np.float64)
        self.highs = np.empty(self.capacity, dtype=np.float64)
        self.lows = np.empty(self.capacity, dtype=np.float64)
        self.closes = np.empty(self.capacity, dtype=np.float64)
        self.volumes = np.empty(self.capacity, dtype=np.float64)
        self.times = np.empty(self.capacity, dtype=np.int64)

    MAX_CAPACITY = 100_000

    def ensure_capacity(self, capacity: int) -> None:
        capacity = max(1, min(int(capacity), self.MAX_CAPACITY))
        if capacity <= self.capacity:
            return
        view = self.tail()
        self.capacity = capacity
        self.start = 0
        self.opens = np.empty(capacity, dtype=np.float64)
        self.highs = np.empty(capacity, dtype=np.float64)
        self.lows = np.empty(capacity, dtype=np.float64)
        self.closes = np.empty(capacity, dtype=np.float64)
        self.volumes = np.empty(capacity, dtype=np.float64)
        self.times = np.empty(capacity, dtype=np.int64)
        self.size = len(view.times)
        self.opens[:self.size] = view.opens
        self.highs[:self.size] = view.highs
        self.lows[:self.size] = view.lows
        self.closes[:self.size] = view.closes
        self.volumes[:self.size] = view.volumes
        self.times[:self.size] = view.times

    def latest_time(self) -> int | None:
        if self.size == 0:
            return None
        return int(self.times[(self.start + self.size - 1) % self.capacity])

    def append(self, candle: dict, open_time: int) -> int:
        dropped = 0
        if self.size < self.capacity:
            index = (self.start + self.size) % self.capacity
            self.size += 1
        else:
            index = self.start
            self.start = (self.start + 1) % self.capacity
            dropped = 1
        self._write_physical(index, candle, open_time)
        return dropped

    def replace(self, logical_index: int, candle: dict, open_time: int | None = None) -> None:
        index = (self.start + int(logical_index)) % self.capacity
        self._write_physical(index, candle, int(self.times[index] if open_time is None else open_time))

    def insert(self, logical_index: int, candle: dict, open_time: int) -> int:
        view = self.tail()
        index = max(0, min(int(logical_index), self.size))
        opens = np.insert(view.opens, index, float(candle.get("open", 0.0)))
        highs = np.insert(view.highs, index, float(candle.get("high", 0.0)))
        lows = np.insert(view.lows, index, float(candle.get("low", 0.0)))
        closes = np.insert(view.closes, index, float(candle.get("close", 0.0)))
        volumes = np.insert(view.volumes, index, float(candle.get("volume", 0.0)))
        times = np.insert(view.times, index, int(open_time))
        overflow = max(0, len(times) - self.capacity)
        if overflow:
            opens = opens[overflow:]
            highs = highs[overflow:]
            lows = lows[overflow:]
            closes = closes[overflow:]
            volumes = volumes[overflow:]
            times = times[overflow:]
        self._reset_from_arrays(opens, highs, lows, closes, volumes, times)
        return overflow

    def trim_to(self, keep: int) -> int:
        keep = int(keep)
        if keep <= 0 or self.size <= keep:
            return 0
        overflow = self.size - keep
        self.start = (self.start + overflow) % self.capacity
        self.size = keep
        return overflow

    def search_time(self, open_time: int) -> tuple[int, bool]:
        times = self.tail().times
        index = int(np.searchsorted(times, int(open_time), side="left"))
        return index, bool(index < len(times) and int(times[index]) == int(open_time))

    def tail(self, n: int = 0) -> CandleArrayView:
        if self.size == 0:
            empty_float = np.empty(0, dtype=np.float64)
            empty_int = np.empty(0, dtype=np.int64)
            return CandleArrayView(empty_float, empty_float, empty_float, empty_float, empty_float, empty_int)
        count = self.size if not n or n <= 0 else min(int(n), self.size)
        offset = self.size - count
        start = (self.start + offset) % self.capacity
        end = start + count
        if end <= self.capacity:
            return CandleArrayView(
                self.opens[start:end],
                self.highs[start:end],
                self.lows[start:end],
                self.closes[start:end],
                self.volumes[start:end],
                self.times[start:end],
            )
        split = end % self.capacity
        return CandleArrayView(
            np.concatenate((self.opens[start:], self.opens[:split])),
            np.concatenate((self.highs[start:], self.highs[:split])),
            np.concatenate((self.lows[start:], self.lows[:split])),
            np.concatenate((self.closes[start:], self.closes[:split])),
            np.concatenate((self.volumes[start:], self.volumes[:split])),
            np.concatenate((self.times[start:], self.times[:split])),
        )

    def tail_field(self, attr: str, n: int = 0) -> np.ndarray:
        view = self.tail(n)
        return {
            "open_list": view.opens,
            "high_list": view.highs,
            "low_list": view.lows,
            "price_list": view.closes,
            "volume_list": view.volumes,
            "time_list": view.times,
        }[attr]

    def _write_physical(self, index: int, candle: dict, open_time: int) -> None:
        self.times[index] = int(open_time)
        self.opens[index] = float(candle.get("open", 0.0))
        self.highs[index] = float(candle.get("high", 0.0))
        self.lows[index] = float(candle.get("low", 0.0))
        self.closes[index] = float(candle.get("close", 0.0))
        self.volumes[index] = float(candle.get("volume", 0.0))

    def _reset_from_arrays(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        volumes: np.ndarray,
        times: np.ndarray,
    ) -> None:
        self.start = 0
        self.size = len(times)
        self.opens[:self.size] = opens
        self.highs[:self.size] = highs
        self.lows[:self.size] = lows
        self.closes[:self.size] = closes
        self.volumes[:self.size] = volumes
        self.times[:self.size] = times


class SharedCandleCache:
    MAX_CAPACITY = 100_000

    def __init__(self, data_max_candles_floor: int = 0):
        self._data: dict[str, dict[str, NumpyCandleBuffer]] = {}
        self._warmup_bars_required: dict[tuple[str, str], int] = {}
        self._retain_bars_required: dict[tuple[str, str], int] = {}
        self._last_updated_at: dict[tuple[str, str], float] = {}
        self._trim_count: dict[tuple[str, str], int] = {}
        self._warmup_baseline_ts: dict[str, int] = {}
        self._tf_version: dict[str, int] = {}
        self.data_max_candles_floor = max(0, int(data_max_candles_floor))

    def register_bars_requirement(self, symbol: str, tf: str, bars: int) -> None:
        self.register_data_requirement(symbol, tf, warmup_bars=bars, retain_bars=bars)

    def register_data_requirement(
        self,
        symbol: str,
        tf: str,
        *,
        warmup_bars: int,
        retain_bars: int | None = None,
        retain_buffer_bars: int = 0,
    ) -> None:
        key = (symbol, tf)
        warmup = max(0, int(warmup_bars))
        retain = warmup if retain_bars is None else max(0, int(retain_bars))
        retain += max(0, int(retain_buffer_bars))
        retain = max(retain, warmup)
        self._warmup_bars_required[key] = max(warmup, self._warmup_bars_required.get(key, 0))
        self._retain_bars_required[key] = max(retain, self._retain_bars_required.get(key, 0))
        sd = self._get_sd(symbol, tf, create=False)
        if sd is not None:
            sd.ensure_capacity(max(1, self.retained_bars(symbol, tf)))
            self._trim(symbol, tf, sd)

    def required_bars(self, symbol: str, tf: str) -> int:
        return self._warmup_bars_required.get((symbol, tf), 0)

    def retained_bars(self, symbol: str, tf: str) -> int:
        key = (symbol, tf)
        return max(
            self.data_max_candles_floor,
            self._retain_bars_required.get(key, self._warmup_bars_required.get(key, 0)),
        )

    def upsert_candle(self, symbol: str, tf: str, candle: dict) -> None:
        if tf in self._warmup_baseline_ts:
            open_time = candle.get("open_time", 0)
            if isinstance(open_time, (int, float)) and open_time < self._warmup_baseline_ts[tf]:
                return
        try:
            open_time = int(candle.get("open_time", candle.get("time", 0)))
        except (TypeError, ValueError):
            return
        if not symbol or not tf or open_time <= 0:
            return

        sd = self._get_sd(symbol, tf, create=True)
        assert sd is not None
        self._ensure_write_capacity(symbol, tf, sd)
        last_time = sd.latest_time()
        if last_time is not None:
            last = last_time
            if open_time == last:
                sd.replace(sd.size - 1, candle, open_time)
                self._last_updated_at[(symbol, tf)] = time.time()
                self._bump_tf_version(tf)
                return
            if open_time > last:
                dropped = sd.append(candle, open_time)
                if dropped:
                    self._trim_count[(symbol, tf)] = self._trim_count.get((symbol, tf), 0) + dropped
                self._trim(symbol, tf, sd)
                self._last_updated_at[(symbol, tf)] = time.time()
                self._bump_tf_version(tf)
                return
            index, exists = sd.search_time(open_time)
            if exists:
                sd.replace(index, candle, open_time)
                self._last_updated_at[(symbol, tf)] = time.time()
                self._bump_tf_version(tf)
                return
        else:
            index = 0

        dropped = sd.insert(index, candle, open_time)
        if dropped:
            self._trim_count[(symbol, tf)] = self._trim_count.get((symbol, tf), 0) + dropped
        self._trim(symbol, tf, sd)
        self._last_updated_at[(symbol, tf)] = time.time()
        self._bump_tf_version(tf)

    def snapshot(self, symbol: str, tf: str, n: int = 0) -> CandleSnapshot:
        return CandleSnapshot(
            opens=self.get_opens(symbol, tf, n),
            highs=self.get_highs(symbol, tf, n),
            lows=self.get_lows(symbol, tf, n),
            closes=self.get_closes(symbol, tf, n),
            volumes=self.get_volumes(symbol, tf, n),
            times=self.get_times(symbol, tf, n),
        )

    def get_closes(self, symbol: str, tf: str, n: int = 0) -> tuple[float, ...]:
        return self._tail_tuple(symbol, tf, "price_list", n)

    def get_opens(self, symbol: str, tf: str, n: int = 0) -> tuple[float, ...]:
        return self._tail_tuple(symbol, tf, "open_list", n)

    def get_highs(self, symbol: str, tf: str, n: int = 0) -> tuple[float, ...]:
        return self._tail_tuple(symbol, tf, "high_list", n)

    def get_lows(self, symbol: str, tf: str, n: int = 0) -> tuple[float, ...]:
        return self._tail_tuple(symbol, tf, "low_list", n)

    def get_volumes(self, symbol: str, tf: str, n: int = 0) -> tuple[float, ...]:
        return self._tail_tuple(symbol, tf, "volume_list", n)

    def get_times(self, symbol: str, tf: str, n: int = 0) -> tuple[int, ...]:
        return self._tail_tuple(symbol, tf, "time_list", n)

    def get_bar_count(self, symbol: str, tf: str) -> int:
        sd = self._get_sd(symbol, tf, create=False)
        return sd.size if sd else 0

    def has_required_bars(self, symbol: str, tf: str, bars: int) -> bool:
        return self.get_bar_count(symbol, tf) >= int(bars)

    def has_fresh_data(self, symbol: str, tf: str, bars: int, max_age_sec: float) -> bool:
        if not self.has_required_bars(symbol, tf, bars):
            return False
        times = self.get_times(symbol, tf, 1)
        if not times:
            return False
        return time.time() - (float(times[-1]) / 1000.0) <= float(max_age_sec)

    def coverage(
        self,
        symbols: Iterable[str],
        tf: str,
        bars: int,
        max_age_sec: float | None = None,
    ) -> tuple[int, int, float]:
        symbol_list = list(symbols)
        loaded = 0
        for symbol in symbol_list:
            ok = self.has_required_bars(symbol, tf, bars)
            if ok and max_age_sec is not None:
                ok = self.has_fresh_data(symbol, tf, bars, max_age_sec)
            if ok:
                loaded += 1
        total = len(symbol_list)
        return loaded, total, (loaded / total if total else 0.0)

    def stats(self) -> list[CacheKeyStats]:
        rows: list[CacheKeyStats] = []
        keys = set(self._warmup_bars_required) | set(self._retain_bars_required) | set(self._trim_count)
        for symbol, by_tf in self._data.items():
            keys.update((symbol, tf) for tf in by_tf)
        for symbol, tf in sorted(keys):
            sd = self._get_sd(symbol, tf, create=False)
            key = (symbol, tf)
            rows.append(CacheKeyStats(
                symbol=symbol,
                tf=tf,
                loaded_bars=sd.size if sd else 0,
                warmup_bars=self.required_bars(symbol, tf),
                retain_bars=self.retained_bars(symbol, tf),
                trim_count=self._trim_count.get(key, 0),
            ))
        return rows

    def set_warmup_baseline(self, tf: str, ts_ms: int) -> None:
        self._warmup_baseline_ts[tf] = ts_ms

    def clear_warmup_baseline(self, tf: str | None = None) -> None:
        if tf is None:
            self._warmup_baseline_ts.clear()
        else:
            self._warmup_baseline_ts.pop(tf, None)

    def get_symbols_with_data(self, tf: str) -> list[str]:
        return sorted(symbol for symbol, by_tf in self._data.items() if tf in by_tf)

    def get_latest_timestamp(self, symbol: str, tf: str) -> int | None:
        sd = self._get_sd(symbol, tf, create=False)
        return None if sd is None else sd.latest_time()

    def tail_arrays(self, symbol: str, tf: str, n: int = 0) -> CandleArrayView | None:
        sd = self._get_sd(symbol, tf, create=False)
        if sd is None or sd.size == 0:
            return None
        return sd.tail(n)

    def get_max_timestamp(self, tf: str) -> int | None:
        max_ts = 0
        for symbol in self.get_symbols_with_data(tf):
            ts = self.get_latest_timestamp(symbol, tf)
            if ts and ts > max_ts:
                max_ts = ts
        return max_ts if max_ts > 0 else None

    def get_tf_version(self, tf: str) -> int:
        return int(self._tf_version.get(tf, 0))

    def trim_tf_to_requirements(
        self,
        tf: str,
        *,
        remove_unrequired: bool = False,
        symbols: Iterable[str] | None = None,
    ) -> None:
        target_symbols = list(symbols) if symbols is not None else list(self._data.keys())
        for symbol in target_symbols:
            sd = self._get_sd(symbol, tf, create=False)
            if sd is None:
                continue
            keep = self.retained_bars(symbol, tf)
            if keep <= 0 and remove_unrequired:
                del self._data[symbol][tf]
                if not self._data[symbol]:
                    del self._data[symbol]
                continue
            self._trim(symbol, tf, sd)

    def _get_sd(self, symbol: str, tf: str, create: bool) -> NumpyCandleBuffer | None:
        if not create:
            return self._data.get(symbol, {}).get(tf)
        by_tf = self._data.setdefault(symbol, {})
        if tf not in by_tf:
            by_tf[tf] = NumpyCandleBuffer(self._initial_capacity(symbol, tf))
        return by_tf[tf]

    def _bump_tf_version(self, tf: str) -> None:
        self._tf_version[tf] = self.get_tf_version(tf) + 1

    def _tail_tuple(self, symbol: str, tf: str, attr: str, n: int) -> tuple:
        sd = self._get_sd(symbol, tf, create=False)
        if sd is None:
            return ()
        return tuple(sd.tail_field(attr, n).tolist())

    def _initial_capacity(self, symbol: str, tf: str) -> int:
        retained = self.retained_bars(symbol, tf)
        return max(1, retained)

    def _ensure_write_capacity(self, symbol: str, tf: str, sd: NumpyCandleBuffer) -> None:
        retained = self.retained_bars(symbol, tf)
        if retained > 0:
            sd.ensure_capacity(min(retained, self.MAX_CAPACITY))
        else:
            default_retain = max(100, sd.size + 1)
            sd.ensure_capacity(min(default_retain, self.MAX_CAPACITY))

    def verify_no_gaps(
        self,
        symbol: str,
        tf: str,
        expected_tf_ms: int | None = None,
    ) -> GapReport:
        sd = self._get_sd(symbol, tf, create=False)
        if sd is None or sd.size < 2:
            return GapReport(
                symbol=symbol,
                tf=tf,
                total_bars=sd.size if sd else 0,
                gap_count=0,
                missing_ranges=(),
                is_clean=True,
            )

        tf_ms = expected_tf_ms if expected_tf_ms is not None else self._infer_tf_ms(tf)
        times = sd.tail().times
        gap_count = 0
        missing_ranges: list[tuple[int, int]] = []

        for i in range(1, len(times)):
            expected = int(times[i - 1]) + tf_ms
            if int(times[i]) > expected:
                gap_count += 1
                missing_ranges.append((expected, int(times[i]) - tf_ms))

        return GapReport(
            symbol=symbol,
            tf=tf,
            total_bars=len(times),
            gap_count=gap_count,
            missing_ranges=tuple(missing_ranges),
            is_clean=(gap_count == 0),
        )

    @staticmethod
    def _infer_tf_ms(tf: str) -> int:
        _TF_MS = {
            "1m": 60_000, "5m": 300_000, "15m": 900_000,
            "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
            "1d": 86_400_000,
        }
        return _TF_MS.get(tf, 60_000)

    def verify_all_no_gaps(
        self,
        tf: str,
        symbols: list[str] | None = None,
    ) -> list[GapReport]:
        if symbols is None:
            symbols = self.get_symbols_with_data(tf)
        return [self.verify_no_gaps(symbol, tf) for symbol in symbols]

    def _trim(self, symbol: str, tf: str, sd: NumpyCandleBuffer) -> None:
        keep = self.retained_bars(symbol, tf)
        if keep <= 0:
            return
        overflow = sd.trim_to(keep)
        if overflow <= 0:
            return
        self._trim_count[(symbol, tf)] = self._trim_count.get((symbol, tf), 0) + overflow
