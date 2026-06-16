from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import Selection, build_panel, select_positions
from runner.strategy.base import Strategy


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
        self._last_processed_candle = 0
        self._pending: tuple[int, Selection] | None = None
        self._open_positions: dict[str, dict[str, Any]] = {}
        self._portfolio_returns: list[float] = []
        self._last_prices: dict[str, float] = {}
        self._base_weights: dict[str, float] = {}
        self._pending_cost = 0.0
        self._strategy_leverage = float(params.get("initial_strategy_leverage", 0.0))

    def get_required_channels(self) -> list[str]:
        return [f"kline:{self.spec.timeframe}"]

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

    async def scan(self) -> None:
        if not self.ctx.state.ready:
            return
        snapshot = self._snapshot()
        if not snapshot:
            return
        latest = max(int(row["time"][-1]) for row in snapshot.values() if row["time"])
        if latest <= self._last_processed_candle:
            return

        prices = {symbol: float(row["close"][-1]) for symbol, row in snapshot.items() if row["close"]}
        self._record_portfolio_return(prices)

        if self._pending and latest >= self._pending[0]:
            _, selection = self._pending
            await self._apply_selection(selection, prices, latest)
            self._pending = None

        bar_number = latest // self._tf_to_ms(self.spec.timeframe)
        is_rebalance = bar_number % self.spec.rebalance_bars == 0
        panel = build_panel(snapshot)
        selection = select_positions(panel, self.spec)
        if is_rebalance:
            execute_at = latest + self.spec.exec_lag * self._tf_to_ms(self.spec.timeframe)
            self._pending = (execute_at, selection)

        self._last_prices = prices
        self._last_processed_candle = latest

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

    def _record_portfolio_return(self, prices: dict[str, float]) -> None:
        if not self._last_prices or not self._base_weights:
            return
        gross = 0.0
        for symbol, weight in self._base_weights.items():
            before = self._last_prices.get(symbol)
            after = prices.get(symbol)
            if before and after:
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
            self._open_positions.pop(symbol, None)

        symbols = set(self._base_weights) | set(selection.weights)
        turnover = sum(abs(selection.weights.get(symbol, 0.0) - self._base_weights.get(symbol, 0.0)) for symbol in symbols)
        self._pending_cost = turnover * self.spec.fee_bps / 10_000
        self._strategy_leverage = self._vol_target_leverage()
        self._base_weights = dict(selection.weights)
        if self._strategy_leverage <= 0 or not self.ctx.can_open_trades():
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
            await self.ctx.emit_signal(
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
