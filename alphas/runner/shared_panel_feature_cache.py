from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from cross_alpha.strategy import CrossAlphaComputeContext, build_panel
from runner.data_layer.cache import SharedCandleCache


@dataclass(frozen=True)
class PanelBundle:
    panel: dict[str, Any]
    context: CrossAlphaComputeContext
    latest: int
    version: int
    universe_hash: str
    bars: int
    lock: threading.RLock


class SharedPanelFeatureCache:
    def __init__(self, max_panels: int = 16):
        self.max_panels = max(1, int(max_panels))
        self._group_max_bars: dict[tuple[str, str], int] = {}
        self._panels: OrderedDict[tuple[int, str, str, int, int], PanelBundle] = OrderedDict()
        self._lock = threading.Lock()
        self.metrics: dict[str, int | float] = {
            "panel_cache_hits": 0,
            "panel_cache_misses": 0,
            "panel_build_total": 0,
            "panel_build_duration_sec_total": 0.0,
            "panel_dense_build_total": 0,
            "panel_fallback_build_total": 0,
            "feature_cache_hits": 0,
            "feature_cache_misses": 0,
            "selection_compute_total": 0,
            "selection_compute_duration_sec_total": 0.0,
        }

    @staticmethod
    def universe_hash(symbols: tuple[str, ...] | list[str]) -> str:
        joined = "\n".join(str(symbol).upper() for symbol in symbols)
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]

    def register_group(self, tf: str, symbols: tuple[str, ...] | list[str], bars: int) -> str:
        universe = self.universe_hash(symbols)
        key = (str(tf), universe)
        current = self._group_max_bars.get(key, 0)
        if int(bars) > current:
            self._group_max_bars[key] = int(bars)
        return universe

    def max_bars(self, tf: str, universe_hash: str, fallback: int) -> int:
        return max(int(fallback), self._group_max_bars.get((str(tf), universe_hash), 0))

    async def get_bundle(
        self,
        cache: SharedCandleCache,
        *,
        tf: str,
        symbols: tuple[str, ...] | list[str],
        bars: int,
    ) -> PanelBundle | None:
        symbol_tuple = tuple(symbols)
        universe = self.register_group(tf, symbol_tuple, bars)
        effective_bars = self.max_bars(tf, universe, bars)
        version = cache.get_tf_version(tf)
        key = (id(cache), str(tf), universe, int(effective_bars), int(version))
        with self._lock:
            cached = self._panels.get(key)
            if cached is not None:
                self._inc("panel_cache_hits")
                self._panels.move_to_end(key)
                return cached

        self._inc("panel_cache_misses")
        started = time.perf_counter()
        panel = await asyncio.to_thread(self._build_panel, cache, tf, symbol_tuple, effective_bars)
        if not panel or panel["close"].empty:
            return None
        latest = int(panel["close"].index.max())
        context = CrossAlphaComputeContext(panel, metrics=self.metrics)
        bundle = PanelBundle(
            panel=panel,
            context=context,
            latest=latest,
            version=version,
            universe_hash=universe,
            bars=effective_bars,
            lock=threading.RLock(),
        )
        with self._lock:
            self._panels[key] = bundle
            while len(self._panels) > self.max_panels:
                self._panels.popitem(last=False)
        self._inc("panel_build_total")
        self.metrics["panel_build_duration_sec_total"] = float(self.metrics["panel_build_duration_sec_total"]) + (
            time.perf_counter() - started
        )
        return bundle

    def clear(self) -> None:
        with self._lock:
            self._group_max_bars.clear()
            self._panels.clear()

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            panel_entries = len(self._panels)
        return {
            **self.metrics,
            "panel_cache_entries": panel_entries,
            "panel_group_entries": len(self._group_max_bars),
        }

    def _build_panel(
        self,
        cache: SharedCandleCache,
        tf: str,
        symbols: tuple[str, ...],
        bars: int,
    ) -> dict[str, pd.DataFrame]:
        dense = self._build_dense_panel(cache, tf, symbols, bars)
        if dense is not None:
            self._inc("panel_dense_build_total")
            return dense
        self._inc("panel_fallback_build_total")
        snapshot = self._build_snapshot(cache, tf, symbols, bars)
        return build_panel(snapshot) if snapshot else {}

    def _build_dense_panel(
        self,
        cache: SharedCandleCache,
        tf: str,
        symbols: tuple[str, ...],
        bars: int,
    ) -> dict[str, pd.DataFrame] | None:
        rows = []
        reference_times: np.ndarray | None = None
        for symbol in symbols:
            view = cache.tail_arrays(symbol, tf, bars)
            if view is None or len(view.times) == 0:
                continue
            if reference_times is None:
                reference_times = view.times
            elif len(view.times) != len(reference_times) or not np.array_equal(view.times, reference_times):
                return None
            rows.append((symbol, view))

        if reference_times is None or not rows:
            return None

        columns = [symbol for symbol, _view in rows]
        index = pd.Index(reference_times.copy(), dtype="int64")

        def frame(field: str) -> pd.DataFrame:
            matrix = np.column_stack([getattr(view, field) for _symbol, view in rows])
            return pd.DataFrame(matrix, index=index, columns=columns, copy=False)

        close = frame("closes")
        high = frame("highs")
        low = frame("lows")
        volume = frame("volumes")
        vwap = (high + low + close) / 3.0
        return {
            "close": close,
            "high": high,
            "low": low,
            "volume": volume,
            "quote_volume": close * volume,
            "vwap": vwap,
        }

    def _build_snapshot(
        self,
        cache: SharedCandleCache,
        tf: str,
        symbols: tuple[str, ...],
        bars: int,
    ) -> dict[str, dict[str, list[float] | list[int]]]:
        out: dict[str, dict[str, list[float] | list[int]]] = {}
        for symbol in symbols:
            snap = cache.snapshot(symbol, tf, bars)
            if not snap.times:
                continue
            out[symbol] = {
                "time": list(snap.times),
                "close": list(snap.closes),
                "high": list(snap.highs),
                "low": list(snap.lows),
                "volume": list(snap.volumes),
            }
        return out

    def _inc(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = int(self.metrics.get(name, 0)) + int(amount)

    def inc(self, name: str, amount: int = 1) -> None:
        self._inc(name, amount)

    def observe_seconds(self, name: str, seconds: float) -> None:
        self.metrics[name] = float(self.metrics.get(name, 0.0)) + float(seconds)
