from __future__ import annotations

import time
from bisect import bisect_left
from dataclasses import dataclass
from typing import Iterable

from base.models import SymbolData


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


class SharedCandleCache:
    def __init__(self, data_max_candles_floor: int = 0):
        self._data: dict[str, dict[str, SymbolData]] = {}
        self._warmup_bars_required: dict[tuple[str, str], int] = {}
        self._retain_bars_required: dict[tuple[str, str], int] = {}
        self._last_updated_at: dict[tuple[str, str], float] = {}
        self._trim_count: dict[tuple[str, str], int] = {}
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
        try:
            open_time = int(candle.get("open_time", candle.get("time", 0)))
        except (TypeError, ValueError):
            return
        if not symbol or not tf or open_time <= 0:
            return

        sd = self._get_sd(symbol, tf, create=True)
        assert sd is not None
        if sd.time_list:
            last = sd.time_list[-1]
            if open_time == last:
                self._replace(sd, len(sd.time_list) - 1, candle)
                self._last_updated_at[(symbol, tf)] = time.time()
                return
            if open_time > last:
                self._append(sd, candle, open_time)
                self._trim(symbol, tf, sd)
                self._last_updated_at[(symbol, tf)] = time.time()
                return
            index = bisect_left(sd.time_list, open_time)
            if index < len(sd.time_list) and sd.time_list[index] == open_time:
                self._replace(sd, index, candle)
                self._last_updated_at[(symbol, tf)] = time.time()
                return
        else:
            index = 0

        sd.time_list.insert(index, open_time)
        sd.open_list.insert(index, float(candle.get("open", 0.0)))
        sd.high_list.insert(index, float(candle.get("high", 0.0)))
        sd.low_list.insert(index, float(candle.get("low", 0.0)))
        sd.price_list.insert(index, float(candle.get("close", 0.0)))
        sd.volume_list.insert(index, float(candle.get("volume", 0.0)))
        self._trim(symbol, tf, sd)
        self._last_updated_at[(symbol, tf)] = time.time()

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
        return self._tail(self._list(symbol, tf, "price_list"), n)

    def get_opens(self, symbol: str, tf: str, n: int = 0) -> tuple[float, ...]:
        return self._tail(self._list(symbol, tf, "open_list"), n)

    def get_highs(self, symbol: str, tf: str, n: int = 0) -> tuple[float, ...]:
        return self._tail(self._list(symbol, tf, "high_list"), n)

    def get_lows(self, symbol: str, tf: str, n: int = 0) -> tuple[float, ...]:
        return self._tail(self._list(symbol, tf, "low_list"), n)

    def get_volumes(self, symbol: str, tf: str, n: int = 0) -> tuple[float, ...]:
        return self._tail(self._list(symbol, tf, "volume_list"), n)

    def get_times(self, symbol: str, tf: str, n: int = 0) -> tuple[int, ...]:
        return self._tail(self._list(symbol, tf, "time_list"), n)

    def get_bar_count(self, symbol: str, tf: str) -> int:
        sd = self._get_sd(symbol, tf, create=False)
        return len(sd.time_list) if sd else 0

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
                loaded_bars=len(sd.time_list) if sd else 0,
                warmup_bars=self.required_bars(symbol, tf),
                retain_bars=self.retained_bars(symbol, tf),
                trim_count=self._trim_count.get(key, 0),
            ))
        return rows

    def _get_sd(self, symbol: str, tf: str, create: bool) -> SymbolData | None:
        if not create:
            return self._data.get(symbol, {}).get(tf)
        return self._data.setdefault(symbol, {}).setdefault(tf, SymbolData())

    def _list(self, symbol: str, tf: str, attr: str) -> list:
        sd = self._get_sd(symbol, tf, create=False)
        return list(getattr(sd, attr)) if sd else []

    @staticmethod
    def _tail(values: list, n: int) -> tuple:
        if n and n > 0:
            values = values[-n:]
        return tuple(values)

    @staticmethod
    def _replace(sd: SymbolData, index: int, candle: dict) -> None:
        sd.open_list[index] = float(candle.get("open", 0.0))
        sd.high_list[index] = float(candle.get("high", 0.0))
        sd.low_list[index] = float(candle.get("low", 0.0))
        sd.price_list[index] = float(candle.get("close", 0.0))
        sd.volume_list[index] = float(candle.get("volume", 0.0))

    @staticmethod
    def _append(sd: SymbolData, candle: dict, open_time: int) -> None:
        sd.time_list.append(open_time)
        sd.open_list.append(float(candle.get("open", 0.0)))
        sd.high_list.append(float(candle.get("high", 0.0)))
        sd.low_list.append(float(candle.get("low", 0.0)))
        sd.price_list.append(float(candle.get("close", 0.0)))
        sd.volume_list.append(float(candle.get("volume", 0.0)))

    def _trim(self, symbol: str, tf: str, sd: SymbolData) -> None:
        keep = self.retained_bars(symbol, tf)
        if keep <= 0:
            return
        overflow = len(sd.time_list) - keep
        if overflow <= 0:
            return
        del sd.time_list[:overflow]
        del sd.open_list[:overflow]
        del sd.high_list[:overflow]
        del sd.low_list[:overflow]
        del sd.price_list[:overflow]
        del sd.volume_list[:overflow]
        self._trim_count[(symbol, tf)] = self._trim_count.get((symbol, tf), 0) + overflow
