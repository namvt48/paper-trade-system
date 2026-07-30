from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

from base.engine import BaseEngine
from base.models import SymbolData
from runner.strategy.base import Strategy

logger = logging.getLogger(__name__)


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.split("#", 1)[0].strip().strip('"').strip("'")
    return values


def _coerce_like(current: Any, value: str) -> Any:
    if isinstance(current, bool):
        return value.lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(value))
    if isinstance(current, float):
        return float(value)
    return value


def _tf_ms(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60_000
    if tf.endswith("h"):
        return int(tf[:-1]) * 3_600_000
    if tf.endswith("d"):
        return int(tf[:-1]) * 86_400_000
    return 60_000


class LegacyStandaloneRunnerStrategy(Strategy):
    """Adapter for the old standalone ``docs/alphas`` engines.

    The old alpha folders all expose ``app.config.settings`` and an Engine class
    that subclasses ``base.engine.BaseEngine``.  They were designed to run in
    separate Python processes, each with its own top-level ``app`` package.  This
    adapter imports each alpha in an isolated temporary ``app`` namespace, feeds
    runner candle-cache snapshots into ``engine.symbol_data``, and converts the
    old sync ``push_signal`` calls into async runner signals.
    """

    def __init__(self, alpha_id: str, version: str, params: dict, ctx):
        super().__init__(alpha_id, version, params, ctx)
        self._alphas_root = Path(__file__).resolve().parents[3]
        self.alpha_dir = self._resolve_alpha_dir(params.get("alpha_dir") or alpha_id)
        self.primary_tf = str(params.get("tf") or params.get("timeframe") or "15m")
        self.htf = str(params.get("htf") or "")
        self.include_1m = bool(params.get("include_1m", True))
        self.scan_on_tfs = tuple(str(x) for x in params.get("scan_on_tfs", [self.primary_tf]))
        self.warmup_bars = int(params.get("warmup_bars", 0) or 0)
        self.retain_bars = int(params.get("retain_bars", self.warmup_bars) or self.warmup_bars)
        self.htf_warmup_bars = int(params.get("htf_warmup_bars", self.warmup_bars) or self.warmup_bars)
        self.htf_retain_bars = int(params.get("htf_retain_bars", self.htf_warmup_bars) or self.htf_warmup_bars)
        self._last_scan_candle_ms: dict[str | tuple[str, str], int] = {}
        self._warmup_hook_done = False
        self._pending_signals: list[tuple[str, dict[str, Any]]] = []

        self.engine = self._load_engine()
        self._supports_per_symbol_scan = callable(getattr(self.engine, "_build_symbol_row", None))
        self._pending_scan_symbol: str | None = None
        self._pending_scan_tf: str | None = None
        if hasattr(self.engine, "_open_positions"):
            self.engine._open_positions = self.reconcile_open_positions()
        self._force_live()

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        tf = str(params.get("tf") or params.get("timeframe") or "15m")
        channels = [f"kline:{tf}"]
        htf = str(params.get("htf") or "")
        if htf:
            channels.append(f"kline:{htf}")
        if bool(params.get("include_1m", True)):
            channels.append("kline:1m")
        return list(dict.fromkeys(channels))

    def get_required_channels_instance(self) -> list[str]:
        return self.__class__.get_required_channels(self.params)

    def get_warmup_symbols(self) -> list[str]:
        configured = self.params.get("symbols")
        if configured:
            if isinstance(configured, str):
                return [s.strip().upper() for s in configured.split(",") if s.strip()]
            return [str(s).upper() for s in configured]
        symbols = self._symbols_from_leverage_file()
        if not symbols:
            symbols = self.engine._get_warmup_symbols()
        max_symbols = int(self.params.get("max_symbols", 0) or 0)
        return symbols[:max_symbols] if max_symbols > 0 else symbols

    def get_warmup_tfs(self) -> list[str]:
        tfs = [self.primary_tf]
        if self.htf:
            tfs.append(self.htf)
        return list(dict.fromkeys(tfs))

    def get_warmup_bars(self, tf: str) -> int:
        if self.htf and tf == self.htf:
            return self.htf_warmup_bars
        return self.warmup_bars or int(getattr(self.engine.config, "WARMUP_BARS", 1))

    def get_retain_bars(self, tf: str) -> int:
        if self.htf and tf == self.htf:
            return self.htf_retain_bars
        return self.retain_bars or self.get_warmup_bars(tf)

    async def _shared_panel_bundle(self):
        return None

    async def on_candle(self, symbol: str, tf: str) -> None:
        self._sync_symbol_tf(symbol, tf)

    async def on_price_alert(self, symbol: str, price: float, side: str) -> None:
        handler = getattr(self.engine, "on_price_alert_message", None)
        if not callable(handler):
            return
        handler({
            "symbol": symbol,
            "price": price,
            "last": price,
            "bid": price,
            "ask": price,
            "side": side,
            "source": "runner",
        })
        await self._flush_signals()
        self._persist_positions()

    def should_scan_after_event(self, kind: str, symbol: str | None = None, tf: str | None = None) -> bool:
        if kind != "kline" or not symbol or not tf or not self.ctx.state.ready:
            return False
        if tf not in self.scan_on_tfs:
            return False
        configured = self.params.get("symbols")
        if configured and symbol.upper() not in {str(s).upper() for s in (configured.split(",") if isinstance(configured, str) else configured)}:
            return False
        latest = self.ctx.cache.get_latest_timestamp(symbol, tf)
        if latest is None:
            return False
        if self.params.get("scan_once_per_tf_candle", True):
            if self._supports_per_symbol_scan:
                key: str | tuple[str, str] = (symbol, tf)
            else:
                key = tf
            if latest <= self._last_scan_candle_ms.get(key, 0):
                return False
            self._last_scan_candle_ms[key] = latest
        if self._supports_per_symbol_scan:
            self._pending_scan_symbol = symbol
            self._pending_scan_tf = tf
        return True

    async def scan(self) -> None:
        if not self.ctx.state.ready:
            return
        symbol = self._pending_scan_symbol if self._supports_per_symbol_scan else None
        tf = self._pending_scan_tf if self._supports_per_symbol_scan else None
        self._pending_scan_symbol = None
        self._pending_scan_tf = None

        if symbol is not None:
            await self._scan_single_symbol(symbol, tf)
        else:
            await self._scan_all_symbols()
        await self._flush_signals()
        self._persist_positions()

    async def _scan_single_symbol(self, symbol: str, tf: str | None) -> None:
        """Scan a single symbol that triggered the event.

        Uses parallel compute path (``_compute_indicators`` on thread pool +
        ``_apply_decision`` on event loop) when the engine supports it.
        Falls back to ``_process_symbol`` or full-batch scan otherwise.
        """
        self._sync_engine_data(symbols=[symbol])
        if not self._warmup_hook_done:
            hook = getattr(self.engine, "on_warmup_complete", None)
            if callable(hook):
                await hook()
            self._warmup_hook_done = True

        build_row = getattr(self.engine, "_build_symbol_row", None)
        if not callable(build_row):
            await self._scan_all_symbols()
            return

        data_lock = getattr(self.engine, "data_lock", None)
        row = None
        if data_lock is not None:
            async with data_lock:
                row = build_row(symbol)
        else:
            row = build_row(symbol)
        if row is None:
            return

        compute_fn = getattr(self.engine, "_compute_indicators", None)
        apply_fn = getattr(self.engine, "_apply_decision", None)
        if callable(compute_fn) and callable(apply_fn):
            # Offload compute to thread pool, apply on event loop
            indic = await asyncio.to_thread(compute_fn, row)
            if indic is not None:
                apply_fn(row, indic)
        else:
            process_one = getattr(self.engine, "_process_symbol", None)
            if callable(process_one):
                process_one(row)

    async def _scan_all_symbols(self) -> None:
        self._sync_engine_data()
        if not self._warmup_hook_done:
            hook = getattr(self.engine, "on_warmup_complete", None)
            if callable(hook):
                await hook()
            self._warmup_hook_done = True

        build_row = getattr(self.engine, "_build_symbol_row", None)
        compute_fn = getattr(self.engine, "_compute_indicators", None)
        apply_fn = getattr(self.engine, "_apply_decision", None)

        if callable(build_row) and callable(compute_fn) and callable(apply_fn):
            # PARALLEL: build rows → compute on thread pool → apply sequentially
            rows = self._build_all_rows(build_row)
            if not rows:
                return
            results = await asyncio.gather(
                *[asyncio.to_thread(compute_fn, row) for row in rows]
            )
            for row, result in zip(rows, results):
                if result is not None:
                    apply_fn(row, result)
        else:
            # FALLBACK: legacy sequential path
            process_all = getattr(self.engine, "_process_all_symbols", None)
            process_one = getattr(self.engine, "_process_symbol", None)
            if callable(process_all):
                await process_all()
            elif callable(process_one):
                await process_one()

    def _build_all_rows(self, build_row) -> list[dict]:
        """Build row dicts for all warmup symbols under data_lock."""
        rows: list[dict] = []
        data_lock = getattr(self.engine, "data_lock", None)
        symbols = self.get_warmup_symbols()
        # data_lock is asyncio.Lock — can't use 'async with' here (sync method).
        # Safe because no other coroutine runs during scan (event loop is
        # single-threaded and _sync_engine_data already completed).
        for symbol in symbols:
            row = build_row(symbol)
            if row is not None:
                rows.append(row)
        return rows

    async def manage_positions(self) -> None:
        if not getattr(self.engine, "_open_positions", None):
            return
        self._sync_engine_data(symbols=list(self.engine._open_positions))
        manager = getattr(self.engine, "_manage_positions", None)
        if callable(manager):
            await manager()
        await self._flush_signals()
        self._persist_positions()

    def _resolve_alpha_dir(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self._alphas_root / path
        if not path.exists():
            raise FileNotFoundError(f"legacy alpha dir not found: {path}")
        return path

    def _symbols_from_leverage_file(self) -> list[str]:
        if self.params.get("symbols_from_leverage", True) is False:
            return []
        path = self.alpha_dir / "data" / "binance_futures_leverage.json"
        if not path.exists():
            return []
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        symbols = []
        for row in rows if isinstance(rows, list) else []:
            symbol = str(row.get("symbol", "")).upper()
            if symbol and not self.engine._is_blacklisted(symbol):
                symbols.append(symbol)
        return sorted(dict.fromkeys(symbols))

    def _load_engine(self) -> BaseEngine:
        previous_path = list(sys.path)
        previous_app_modules = {
            name: module for name, module in sys.modules.items()
            if name == "app" or name.startswith("app.")
        }
        for name in list(previous_app_modules):
            sys.modules.pop(name, None)
        sys.path.insert(0, str(self.alpha_dir))
        sys.path.insert(0, str(self._alphas_root))
        old_cwd = Path.cwd()
        try:
            os.chdir(self.alpha_dir)
            config_module = importlib.import_module("app.config")
            settings = config_module.settings
            self._apply_settings(settings)
            engine_module = importlib.import_module("app.engine")
            engine_cls = self._find_engine_class(engine_module)
            engine = engine_cls()
            engine.push_signal = self._capture_signal  # type: ignore[method-assign]
            return engine
        finally:
            os.chdir(old_cwd)
            for name in list(sys.modules):
                if name == "app" or name.startswith("app."):
                    sys.modules.pop(name, None)
            sys.modules.update(previous_app_modules)
            sys.path[:] = previous_path

    def _apply_settings(self, settings: Any) -> None:
        values = _parse_env_file(self.alpha_dir / ".env")
        values.update({str(k).upper(): str(v) for k, v in (self.params.get("env") or {}).items()})
        values.setdefault("ALPHA_ID", self.alpha_id)
        values.setdefault("TF", self.primary_tf)
        if self.htf:
            values.setdefault("HTF", self.htf)
        values.setdefault("REDIS_URL", str(self.params.get("redis_url", "redis://paper-redis:6379")))
        values.setdefault("REDIS_STREAM", str(self.params.get("signal_stream", "paper-signals")))
        values.setdefault("MDS_REDIS_URL", str(self.params.get("mds_redis_url", "redis://mds-redis:6379")))
        values.setdefault("MDS_EXCHANGE", str(self.params.get("exchange", "binance")))
        if self.warmup_bars:
            values.setdefault("WARMUP_BARS", str(self.warmup_bars))

        for key, raw_value in values.items():
            if not hasattr(settings, key):
                continue
            current = getattr(settings, key)
            try:
                setattr(settings, key, _coerce_like(current, raw_value))
            except Exception:
                setattr(settings, key, raw_value)

        for attr, filename in (("LEVERAGE_FILE", "data/binance_futures_leverage.json"), ("BLACKLIST_FILE", "blacklist.txt")):
            if not hasattr(settings, attr):
                continue
            raw = str(getattr(settings, attr) or "")
            if not raw:
                continue
            if raw.startswith("/app/data/"):
                path = self.alpha_dir / "data" / Path(raw).name
            elif raw == "/app/blacklist.txt":
                path = self.alpha_dir / "blacklist.txt"
            else:
                path = Path(raw)
                if not path.is_absolute():
                    path = self.alpha_dir / path
            setattr(settings, attr, str(path))

    @staticmethod
    def _find_engine_class(module: Any):
        candidates = []
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, BaseEngine) and value is not BaseEngine:
                candidates.append(value)
        if not candidates:
            raise RuntimeError(f"No BaseEngine subclass found in {module!r}")
        candidates.sort(key=lambda cls: cls.__name__)
        return candidates[0]

    def _force_live(self) -> None:
        self.engine.runtime_state = "LIVE"
        self.engine._data_stale = False
        self.engine._position_reconcile_stale = False
        self.engine._price_alert_sync_stale = False
        self.engine._heartbeat_ok = True

    def _sync_engine_data(self, symbols: list[str] | None = None) -> None:
        target_symbols = symbols or self.get_warmup_symbols()
        for symbol in target_symbols:
            for tf in self.get_required_channels_instance():
                if not tf.startswith("kline:"):
                    continue
                self._sync_symbol_tf(symbol, tf.rsplit(":", 1)[-1])

    def _sync_symbol_tf(self, symbol: str, tf: str) -> None:
        bars = self.get_retain_bars(tf) if tf in self.get_warmup_tfs() else int(self.params.get("retain_1m_bars", 1000))
        snap = self.ctx.cache.snapshot(symbol, tf, bars)
        if not snap.times:
            return
        self.engine.symbol_data.setdefault(symbol, {})[tf] = SymbolData(
            price_list=list(snap.closes),
            volume_list=list(snap.volumes),
            high_list=list(snap.highs),
            low_list=list(snap.lows),
            open_list=list(snap.opens),
            time_list=list(snap.times),
        )

    def _capture_signal(self, signal_type: str, **fields: Any) -> None:
        fields = dict(fields)
        self._enrich_signal_fields(fields)
        self._pending_signals.append((signal_type, fields))

    def _enrich_signal_fields(self, fields: dict[str, Any]) -> None:
        position_id = str(fields.get("position_id") or "")
        position = None
        if position_id:
            for symbol, pos in getattr(self.engine, "_open_positions", {}).items():
                if str(pos.get("position_id")) == position_id:
                    position = pos
                    fields.setdefault("symbol", symbol)
                    break
        fields.setdefault("tf", self.primary_tf)
        if position:
            fields.setdefault("side", position.get("side"))
            fields.setdefault("entry", position.get("entry"))
            fields.setdefault("signal_candle_open_ms", position.get("entry_candle_open_ms"))
        if "exchange" not in fields:
            fields["exchange"] = str(getattr(self.engine.config, "EXCHANGE", "binance"))
        if "fee_pct" not in fields and hasattr(self.engine.config, "FEE_PCT"):
            fields["fee_pct"] = getattr(self.engine.config, "FEE_PCT")

    async def _flush_signals(self) -> None:
        while self._pending_signals:
            signal_type, fields = self._pending_signals.pop(0)
            await self.ctx.emit_signal(signal_type, **fields)

    def _persist_positions(self) -> None:
        positions = getattr(self.engine, "_open_positions", {})
        if positions:
            self.ctx.save_positions(positions)
        else:
            self.ctx.clear_positions()
