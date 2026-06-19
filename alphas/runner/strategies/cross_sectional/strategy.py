from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import Selection, select_positions
from runner.shared_panel_feature_cache import PanelBundle, SharedPanelFeatureCache
from runner.strategy.base import Strategy

logger = logging.getLogger(__name__)


class CrossSectionalRunnerStrategy(Strategy):
    def __init__(self, alpha_id: str, version: str, params: dict, ctx):
        super().__init__(alpha_id, version, params, ctx)
        self._alphas_root = Path(__file__).resolve().parents[3]
        self.spec = AlphaSpec.load(self._required_path("spec_file"))
        self.exchange = str(params.get("exchange", "binance"))
        self.capital = float(params.get("capital", 10_000.0))
        self.offset_candle_sec = float(params.get("offset_candle_sec", 5.0))
        self.retain_buffer_bars = int(params.get("retain_buffer_bars", 0))
        self._symbols = self._load_universe()
        self._symbol_set = set(self._symbols)
        self.scan_min_symbol_coverage = float(
            params.get("scan_min_symbol_coverage", self.ctx.warmup_min_symbol_coverage)
        )
        if self.ctx.panel_feature_cache is None:
            self.ctx.panel_feature_cache = SharedPanelFeatureCache()
        self.ctx.panel_feature_cache.register_group(
            self.spec.timeframe,
            tuple(self._symbols),
            self.get_warmup_bars(self.spec.timeframe),
        )
        self._last_processed_candle = 0
        self._pending: tuple[int, Selection] | None = None
        self._open_positions: dict[str, dict[str, Any]] = self.ctx.load_positions()
        self._portfolio_returns: list[float] = []
        self._last_prices: dict[str, float] = {}
        self._base_weights: dict[str, float] = {}
        self._pending_cost = 0.0
        self._strategy_leverage = float(params.get("initial_strategy_leverage", 0.0))
        self._last_pnl_publish: dict[str, float] = {}
        self._pnl_channel = f"pnl:{self.alpha_id}"

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        tf = params.get("timeframe")
        if not tf:
            spec_path = params.get("spec_file")
            if spec_path:
                try:
                    from cross_alpha.spec import AlphaSpec
                    spec = AlphaSpec.load(cls._resolve_spec_path(spec_path, params))
                    tf = spec.timeframe
                except Exception:
                    pass
        if not tf:
            tf = "15m"
        return [f"kline:{tf}"]

    @staticmethod
    def _resolve_spec_path(spec_file: str, params: dict) -> Path:
        p = Path(spec_file)
        if p.is_absolute() and p.exists():
            return p
        if p.exists():
            return p
        alphas_root = Path(__file__).resolve().parents[3]
        return alphas_root / spec_file

    def get_required_channels_instance(self) -> list[str]:
        return self.__class__.get_required_channels(self.params)

    def get_warmup_symbols(self) -> list[str]:
        return list(self._symbols)

    def get_warmup_tfs(self) -> list[str]:
        return [self.spec.timeframe]

    def get_warmup_bars(self, tf: str) -> int:
        return int(self.params.get("warmup_bars", self.spec.required_bars))

    def get_retain_bars(self, tf: str) -> int:
        return int(self.params.get("retain_bars", self.get_warmup_bars(tf)))

    def get_retain_buffer_bars(self, tf: str) -> int:
        return self.retain_buffer_bars

    async def on_candle(self, symbol: str, tf: str) -> None:
        return None

    async def on_price_alert(self, symbol: str, price: float, side: str) -> None:
        pos = self._open_positions.get(symbol)
        if pos is None or price <= 0:
            return
        now = time.time()
        if now - self._last_pnl_publish.get(symbol, 0.0) < 0.5:
            return
        entry = float(pos.get("entry", 0.0))
        if entry <= 0:
            return
        pos_side = str(pos.get("side", ""))
        if pos_side == "LONG":
            pnl_pct = (price - entry) / entry
        elif pos_side == "SHORT":
            pnl_pct = (entry - price) / entry
        else:
            return
        payload = json.dumps({
            "alpha_id": self.alpha_id,
            "symbol": symbol,
            "side": pos_side,
            "entry_price": entry,
            "current_price": price,
            "pnl_pct": round(pnl_pct, 6),
            "weight": float(pos.get("weight", 0.0)),
            "timestamp": int(now * 1000),
        })
        rc = self.ctx.redis_client
        if rc is not None:
            try:
                rc.publish(self._pnl_channel, payload)
            except Exception:
                logger.debug("[PNL] publish failed for %s", symbol, extra={"alpha_id": self.alpha_id})
        self._last_pnl_publish[symbol] = now

    def should_scan_after_event(self, kind: str, symbol: str | None = None, tf: str | None = None) -> bool:
        if kind != "kline" or tf != self.spec.timeframe or not symbol:
            return False
        if symbol not in self._symbol_set:
            return False
        candle_open_ms = self.ctx.cache.get_latest_timestamp(symbol, self.spec.timeframe)
        if candle_open_ms is None or candle_open_ms <= self._last_processed_candle:
            return False
        return self._candle_coverage(candle_open_ms) >= self.scan_min_symbol_coverage

    async def scan(self) -> None:
        if not self.ctx.state.ready:
            return
        bundle = await self._shared_panel_bundle()
        if bundle is None:
            return
        panel = bundle.panel
        compute_context = bundle.context
        latest = bundle.latest
        if latest <= self._last_processed_candle:
            return

        prices = {
            str(symbol): float(price)
            for symbol, price in panel["close"].ffill().iloc[-1].dropna().items()
        }
        self._record_portfolio_return(prices)

        if self._pending and latest >= self._pending[0]:
            _, selection = self._pending
            await self._apply_selection(selection, prices, latest)
            self._pending = None

        bar_number = latest // self._tf_to_ms(self.spec.timeframe)
        is_rebalance = bar_number % self.spec.rebalance_bars == 0
        started = time.perf_counter()
        selection = await asyncio.to_thread(self._select_positions, bundle)
        if self.ctx.panel_feature_cache is not None:
            self.ctx.panel_feature_cache.inc("selection_compute_total")
            self.ctx.panel_feature_cache.observe_seconds(
                "selection_compute_duration_sec_total",
                time.perf_counter() - started,
            )
        sample_symbol, sample_indicator = self._audit_sample(selection)
        logger.info(
            "[SIGNAL_AUDIT] %s",
            json.dumps({
                "alpha_id": self.spec.alpha_id,
                "timeframe": self.spec.timeframe,
                "signal_candle_open_ms": latest,
                "is_rebalance": is_rebalance,
                "signal": self.spec.signal,
                "params": self.spec.params,
                "long_threshold": self.spec.long_threshold,
                "short_threshold": self.spec.short_threshold,
                "symbol_count": len(selection.indicators),
                "long_count": len(selection.longs),
                "short_count": len(selection.shorts),
                "sample_symbol": sample_symbol,
                "sample_indicator": sample_indicator,
            }, separators=(",", ":"), allow_nan=False),
            extra={"alpha_id": self.alpha_id},
        )
        if is_rebalance:
            execute_at = latest + self.spec.exec_lag * self._tf_to_ms(self.spec.timeframe)
            self._pending = (execute_at, selection)
            logger.info(
                "[%s] Decision queued for %d: long=%d short=%d gross=%.3f net=%.6f",
                self.spec.alpha_id,
                execute_at,
                len(selection.longs),
                len(selection.shorts),
                selection.diagnostics["gross"],
                selection.diagnostics["net"],
                extra={"alpha_id": self.alpha_id},
            )

        self._last_prices = prices
        self._last_processed_candle = latest

    def _select_positions(self, bundle: PanelBundle) -> Selection:
        with bundle.lock:
            return select_positions(bundle.panel, self.spec, context=bundle.context)

    @staticmethod
    def _audit_sample(selection: Selection) -> tuple[str | None, dict[str, Any] | None]:
        for symbol in sorted(selection.indicators):
            return symbol, selection.indicators[symbol]
        return None, None

    def _required_path(self, param: str) -> Path:
        value = self.params.get(param)
        if not value:
            raise ValueError(f"{self.alpha_id} missing required params.{param}")
        path = self._resolve_path(str(value))
        if not path.exists():
            raise FileNotFoundError(path)
        return path

    def _resolve_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        if path.exists():
            return path
        return self._alphas_root / path

    def _load_universe(self) -> list[str]:
        path_value = self.params.get("universe_file")
        symbols = list(self.params.get("symbols") or [])
        if path_value:
            with open(self._resolve_path(str(path_value)), encoding="utf-8") as fh:
                loaded = json.load(fh)
            symbols = list(loaded.get("symbols", []))
        blacklist = self._load_blacklist()
        clean = []
        for symbol in symbols:
            symbol = str(symbol).strip().upper()
            if symbol and symbol not in blacklist:
                clean.append(symbol)
        if not clean:
            raise ValueError(f"{self.alpha_id} has no symbols after universe/blacklist load")
        return clean

    def _load_blacklist(self) -> set[str]:
        path_value = self.params.get("blacklist_file")
        if not path_value:
            return set()
        path = self._resolve_path(str(path_value))
        if not path.exists():
            return set()
        return {
            line.strip().upper()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    def _snapshot(self) -> dict[str, dict[str, list[float] | list[int]]]:
        out: dict[str, dict[str, list[float] | list[int]]] = {}
        bars = self.get_warmup_bars(self.spec.timeframe)
        for symbol in self._symbols:
            snap = self.ctx.cache.snapshot(symbol, self.spec.timeframe, bars)
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

    async def _shared_panel_bundle(self) -> PanelBundle | None:
        if self.ctx.panel_feature_cache is None:
            self.ctx.panel_feature_cache = SharedPanelFeatureCache()
        return await self.ctx.panel_feature_cache.get_bundle(
            self.ctx.cache,
            tf=self.spec.timeframe,
            symbols=tuple(self._symbols),
            bars=self.get_warmup_bars(self.spec.timeframe),
        )

    def _latest_cached_timestamp(self, tf: str, symbols: tuple[str, ...]) -> int:
        latest = 0
        for symbol in symbols:
            ts = self.ctx.cache.get_latest_timestamp(symbol, tf)
            if ts is not None and int(ts) > latest:
                latest = int(ts)
        return latest

    @classmethod
    def clear_shared_compute_cache(cls) -> None:
        return None

    def _candle_coverage(self, candle_open_ms: int) -> float:
        if not self._symbols:
            return 0.0
        loaded = 0
        for symbol in self._symbols:
            ts = self.ctx.cache.get_latest_timestamp(symbol, self.spec.timeframe)
            if ts is not None and int(ts) >= int(candle_open_ms):
                loaded += 1
        return loaded / len(self._symbols)

    def _record_portfolio_return(self, prices: dict[str, float]) -> None:
        if not self._last_prices or not self._base_weights:
            return
        gross = 0.0
        for symbol, weight in self._base_weights.items():
            before = self._last_prices.get(symbol)
            after = prices.get(symbol)
            if before and after and before > 0:
                gross += weight * (after / before - 1.0)
        self._portfolio_returns.append(gross - self._pending_cost)
        self._pending_cost = 0.0
        self._portfolio_returns = self._portfolio_returns[-max(self.spec.vol_lookback * 2, 10):]

    def _vol_target_leverage(self) -> float:
        minimum = max(2, self.spec.vol_lookback // 2)
        values = self._portfolio_returns[-self.spec.vol_lookback:]
        if len(values) < minimum:
            return self._strategy_leverage
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
        rv = math.sqrt(variance) * math.sqrt(self.spec.ppy)
        if rv <= 0:
            return 0.0
        return min(self.spec.max_leverage, self.spec.target_vol / rv)

    async def _apply_selection(self, selection: Selection, prices: dict[str, float], candle_open_ms: int) -> None:
        for symbol, pos in list(self._open_positions.items()):
            await self.ctx.emit_signal(
                "CLOSE",
                symbol=symbol,
                tf=self.spec.timeframe,
                position_id=pos["position_id"],
                exit_price=prices.get(symbol, pos["entry"]),
                reason="REBALANCE",
                signal_candle_open_ms=candle_open_ms,
            )
        self._open_positions.clear()

        symbols = set(self._base_weights) | set(selection.weights)
        turnover = sum(abs(selection.weights.get(symbol, 0.0) - self._base_weights.get(symbol, 0.0)) for symbol in symbols)
        self._pending_cost = turnover * self.spec.fee_bps / 10_000
        self._strategy_leverage = self._vol_target_leverage()
        self._base_weights = dict(selection.weights)
        if self._strategy_leverage <= 0 or not self.ctx.can_open_trades():
            self.ctx.save_positions(self._open_positions)
            return

        for symbol, weight in selection.weights.items():
            price = prices.get(symbol)
            if not price or price <= 0:
                continue
            side = "LONG" if weight > 0 else "SHORT"
            notional = self.capital * abs(weight) * self._strategy_leverage
            position_id = f"{self.alpha_id}:{symbol}:{side}:{candle_open_ms}"
            pos = {
                "position_id": position_id,
                "symbol": symbol,
                "side": side,
                "entry": price,
                "qty": notional / price,
                "weight": weight,
                "strategy_leverage": self._strategy_leverage,
                "entry_candle_open_ms": candle_open_ms,
            }
            self._open_positions[symbol] = pos
            result = await self.ctx.emit_signal(
                "OPEN",
                symbol=symbol,
                side=side,
                tf=self.spec.timeframe,
                entry=price,
                qty=pos["qty"],
                leverage=1,
                position_id=position_id,
                exchange=self.exchange,
                fee_pct=self.spec.fee_bps / 10_000,
                metadata=json.dumps({
                    "score": selection.scores.get(symbol),
                    "rank": selection.ranks.get(symbol),
                    "weight": weight,
                    "strategy_leverage": self._strategy_leverage,
                    **selection.diagnostics,
                }),
                signal_candle_open_ms=candle_open_ms,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            if result is None and not self.ctx.state.lease_valid:
                self._open_positions.pop(symbol, None)

        self.ctx.save_positions(self._open_positions)

    @staticmethod
    def _tf_to_ms(tf: str) -> int:
        unit = tf[-1]
        value = int(tf[:-1])
        if unit == "m":
            return value * 60_000
        if unit == "h":
            return value * 3_600_000
        if unit == "d":
            return value * 86_400_000
        raise ValueError(f"unsupported timeframe: {tf}")
