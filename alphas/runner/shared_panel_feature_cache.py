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
from runner.perf_metrics import LabeledLatencyWindows, LatencyWindow


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
        self._inflight: dict[tuple[int, str, str, int, int], asyncio.Task] = {}
        self._lock = threading.Lock()
        self._panel_build_latency: LatencyWindow = LatencyWindow()
        self._selection_compute_latency: LatencyWindow = LatencyWindow()
        self._panel_build_by_group: LabeledLatencyWindows = LabeledLatencyWindows()
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
            "panel_build_single_flight_joins_total": 0,
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
        """Fetch (building if needed) the shared panel for ``(tf, universe,
        bars, version)``.

        Single-flight: when several alphas share the same timeframe and
        universe (e.g. all the ``1d`` cross-sectional alphas), a universe
        refresh previously made each one independently call
        ``asyncio.to_thread(self._build_panel, ...)`` for the identical
        underlying data -- N redundant builds competing for the runner's
        shared compute thread pool at once. Now only the first caller for
        a given key builds; concurrent callers for the same key await that
        one build instead of starting their own (2026-07-16 incident, see
        .agents/PLAN.md U5).
        """
        symbol_tuple = tuple(symbols)
        universe = self.register_group(tf, symbol_tuple, bars)
        effective_bars = self.max_bars(tf, universe, bars)
        version = cache.get_tf_version(tf)
        key = (id(cache), str(tf), universe, int(effective_bars), int(version))

        owns_build = False
        with self._lock:
            cached = self._panels.get(key)
            if cached is not None:
                self._inc("panel_cache_hits")
                self._panels.move_to_end(key)
                return cached

            inflight = self._inflight.get(key)
            if inflight is None:
                self._inc("panel_cache_misses")
                inflight = asyncio.ensure_future(
                    self._build_and_store(cache, tf, symbol_tuple, effective_bars, key, version, universe)
                )
                self._inflight[key] = inflight
                owns_build = True
            else:
                self._inc("panel_build_single_flight_joins_total")

        try:
            return await inflight
        finally:
            if owns_build:
                with self._lock:
                    self._inflight.pop(key, None)

    async def _build_and_store(
        self,
        cache: SharedCandleCache,
        tf: str,
        symbols: tuple[str, ...],
        effective_bars: int,
        key: tuple[int, str, str, int, int],
        version: int,
        universe: str,
    ) -> PanelBundle | None:
        started = time.perf_counter()
        panel = await asyncio.to_thread(self._build_panel, cache, tf, symbols, effective_bars)
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
        duration_sec = time.perf_counter() - started
        self._inc("panel_build_total")
        self.observe_panel_build(tf, universe, duration_sec)
        return bundle

    def clear(self) -> None:
        with self._lock:
            self._group_max_bars.clear()
            self._panels.clear()

    def snapshot(self) -> dict[str, Any]:
        """Return cache counters plus bounded latency summaries."""
        with self._lock:
            panel_entries = len(self._panels)
        return {
            **self.metrics,
            "panel_cache_entries": panel_entries,
            "panel_group_entries": len(self._group_max_bars),
            "latency": {
                "panel_build_sec": self._panel_build_latency.snapshot(),
                "selection_compute_sec": self._selection_compute_latency.snapshot(),
                "panel_build_by_group_sec": self._panel_build_by_group.snapshot(),
            },
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
        """Add a cumulative duration and update its bounded latency window."""
        self.metrics[name] = float(self.metrics.get(name, 0.0)) + float(seconds)
        if name == "selection_compute_duration_sec_total":
            self._selection_compute_latency.observe(seconds)

    def observe_panel_build(self, tf: str, universe_hash: str, seconds: float) -> None:
        """Record overall and cardinality-bounded panel-build latency."""
        self.metrics["panel_build_duration_sec_total"] = (
            float(self.metrics["panel_build_duration_sec_total"]) + float(seconds)
        )
        self._panel_build_latency.observe(seconds)
        self._panel_build_by_group.observe(f"{tf}:{universe_hash}", seconds)
