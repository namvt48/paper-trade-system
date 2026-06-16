from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from base.symbol_utils import get_binance_perp_symbols, get_binance_perp_symbols_by_volume_rank
from base.v5_indicators import compute_v5_tail_indicators
from runner.strategy.base import Strategy


def _tf_ms(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60_000
    if tf.endswith("h"):
        return int(tf[:-1]) * 3_600_000
    if tf.endswith("d"):
        return int(tf[:-1]) * 86_400_000
    raise ValueError(f"unsupported timeframe: {tf}")


def _calc_net_pnl(side: str, entry: float, exit_price: float, size: float, fee_rate: float) -> float:
    qty = size / entry
    gross = qty * (exit_price - entry) if side == "LONG" else qty * (entry - exit_price)
    return gross - fee_rate * size - fee_rate * (qty * exit_price)


def _load_symbols_from_leverage_file(path: Path, start: int | None = None, end: int | None = None) -> list[str]:
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    symbols = [str(row["symbol"]).upper() for row in rows if isinstance(row, dict) and row.get("symbol")]
    return symbols[slice(start, end)]


class V5TailRunnerStrategy(Strategy):
    def __init__(self, alpha_id: str, version: str, params: dict, ctx):
        super().__init__(alpha_id, version, params, ctx)
        self._alphas_root = Path(__file__).resolve().parents[3]
        self.tf = str(params.get("tf", "15m"))
        self.exchange = str(params.get("exchange", "binance"))
        self.sma_len = int(params.get("sma_len", 50))
        self.atr_len = int(params.get("atr_len", 200))
        self.poc_len = int(params.get("poc_len", 30))
        self.norm_window = int(params.get("norm_window", 252))
        self.threshold = float(params.get("threshold", 0.15))
        self.trail_atr_min = float(params.get("trail_atr_min", 0.45))
        self.trail_atr_max = float(params.get("trail_atr_max", 0.55))
        self.tp_ratio = float(params.get("tp_ratio", 2.0))
        self.poc_filter_pct = float(params.get("poc_filter_pct", 0.02))
        self.initial_fixed_tp_sl_pct = params.get("initial_fixed_tp_sl_pct")
        self.initial_fixed_tp_sl_pct = None if self.initial_fixed_tp_sl_pct is None else float(self.initial_fixed_tp_sl_pct)
        self.reverse_side = bool(params.get("reverse_side", False))
        self.capital = float(params.get("capital", 10_000.0))
        self.invest_per_trade = float(params.get("invest_per_trade", 1_000.0))
        self.min_invest = float(params.get("min_invest", 500.0))
        self.scale_factor = float(params.get("scale_factor", 0.30))
        self.kelly_lookback = int(params.get("kelly_lookback", 20))
        self.kelly_base_wr = float(params.get("kelly_base_wr", 0.5))
        self.max_trade_bars = int(params.get("max_trade_bars", 500))
        self.min_hold_bars = int(params.get("min_hold_bars", 4))
        self.default_leverage = int(params.get("leverage", 10))
        self.max_concurrent_positions = int(params.get("max_concurrent_positions", 50))
        self.warmup_bars = int(params.get("warmup_bars", 400))
        self.retain_bars = int(params.get("retain_bars", self.warmup_bars))
        self.fee_pct = float(params.get("fee_pct", 0.0005))
        self._leverage_file = self._resolve_optional_path(params.get("leverage_file"))
        self._symbols_file = self._resolve_optional_path(params.get("symbols_file"))
        self._blacklist_file = self._resolve_optional_path(params.get("blacklist_file"))
        self._volume_rank_start = params.get("volume_rank_start")
        self._volume_rank_end = params.get("volume_rank_end")
        self._leverage_map = self._load_leverage_map()
        self._blacklist = self._load_blacklist()
        self._symbols = self._load_symbols()
        self._open_positions: dict[str, dict] = {}
        self._trend_state: dict[str, Optional[bool]] = {}
        self._last_processed_candle: dict[str, int] = {}
        self._cur_eq = self.capital
        self._cur_size = self.invest_per_trade
        self._recent_results: list[bool] = []

    def get_required_channels(self) -> list[str]:
        return [f"kline:{self.tf}"]

    def get_warmup_symbols(self) -> list[str]:
        return list(self._symbols)

    def get_warmup_tfs(self) -> list[str]:
        return [self.tf]

    def get_warmup_bars(self, tf: str) -> int:
        return self.warmup_bars

    def get_retain_bars(self, tf: str) -> int:
        return self.retain_bars

    async def on_candle(self, symbol: str, tf: str) -> None:
        return None

    async def scan(self) -> None:
        if not self.ctx.state.ready:
            return
        for symbol in self._symbols:
            await self._process_symbol(symbol)

    async def on_price_alert(self, symbol: str, price: float, side: str) -> None:
        symbol = symbol.upper()
        pos = self._open_positions.get(symbol)
        if not pos or self._bars_held(pos, int(datetime.now(timezone.utc).timestamp() * 1000)) < self.min_hold_bars:
            return
        await self._manage_existing_position(symbol, pos, close=price, high=price, low=price, current_time_ms=0)

    def _resolve_optional_path(self, value) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        if path.is_absolute():
            return path
        if path.exists():
            return path
        return self._alphas_root / path

    def _load_leverage_map(self) -> dict[str, int]:
        if self._leverage_file is None or not self._leverage_file.exists():
            return {}
        rows = json.loads(self._leverage_file.read_text(encoding="utf-8"))
        return {
            str(row["symbol"]).upper(): int(row["max_leverage"])
            for row in rows
            if isinstance(row, dict) and row.get("symbol") and row.get("max_leverage")
        }

    def _load_blacklist(self) -> set[str]:
        if self._blacklist_file is None or not self._blacklist_file.exists():
            return set()
        return {
            line.strip().upper()
            for line in self._blacklist_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    def _load_symbols(self) -> tuple[str, ...]:
        configured = [str(s).upper() for s in self.params.get("symbols", []) if str(s).strip()]
        if configured:
            symbols = configured
        elif self._symbols_file is not None:
            symbols = _load_symbols_from_leverage_file(
                self._symbols_file,
                None if self._volume_rank_start is None else int(self._volume_rank_start),
                None if self._volume_rank_end is None else int(self._volume_rank_end),
            )
        elif self._volume_rank_start is not None and self._volume_rank_end is not None:
            symbols = get_binance_perp_symbols_by_volume_rank(int(self._volume_rank_start), int(self._volume_rank_end))
            if not symbols and self._leverage_file is not None:
                symbols = _load_symbols_from_leverage_file(
                    self._leverage_file,
                    int(self._volume_rank_start),
                    int(self._volume_rank_end),
                )
        else:
            symbols = get_binance_perp_symbols()
            if not symbols and self._leverage_file is not None:
                symbols = _load_symbols_from_leverage_file(self._leverage_file)
        symbols = [symbol for symbol in symbols if symbol and symbol not in self._blacklist]
        if not symbols:
            raise ValueError(f"{self.alpha_id} has no symbols")
        return tuple(symbols)

    def _symbol_leverage(self, symbol: str) -> int:
        return self._leverage_map.get(symbol, self.default_leverage)

    def _adaptive_trail(self, acol: float, atr: float) -> float:
        strength = min(1.0, max(0.0, abs(acol)))
        mult = self.trail_atr_max - strength * (self.trail_atr_max - self.trail_atr_min)
        return mult * atr

    def _kelly_size(self) -> float:
        if len(self._recent_results) < 5:
            return self._cur_size
        recent = self._recent_results[-self.kelly_lookback:]
        wr = sum(recent) / len(recent)
        multiplier = min(2.0, max(0.5, wr / self.kelly_base_wr))
        return max(self.min_invest, min(self._cur_size * multiplier, self.scale_factor * self._cur_eq))

    def _bars_held(self, pos: dict, current_candle_open_ms: int) -> int:
        entry_ms = int(pos.get("entry_candle_open_ms", current_candle_open_ms))
        return max(0, (int(current_candle_open_ms) - entry_ms) // _tf_ms(self.tf))

    async def _process_symbol(self, symbol: str) -> None:
        snap = self.ctx.cache.snapshot(symbol, self.tf, self.retain_bars)
        if len(snap.closes) < self.warmup_bars:
            return
        signal_open_time_ms = int(snap.times[-1])
        if self._last_processed_candle.get(symbol) == signal_open_time_ms:
            return
        indic = compute_v5_tail_indicators(
            list(snap.closes),
            list(snap.highs),
            list(snap.lows),
            sma_len=self.sma_len,
            atr_len=self.atr_len,
            poc_len=self.poc_len,
            norm_window=self.norm_window,
        )
        if indic is None:
            return
        self._last_processed_candle[symbol] = signal_open_time_ms
        await self._apply_indicator(symbol, indic, signal_open_time_ms)

    async def _apply_indicator(self, symbol: str, indic: dict, signal_open_time_ms: int) -> None:
        acol = float(indic["acol"])
        acol_prev = float(indic["acol_prev"])
        atr = float(indic["atr"])
        poc = float(indic["poc"])
        close = float(indic["close"])
        high = float(indic["high"])
        low = float(indic["low"])

        old_trend = self._trend_state.get(symbol)
        trend = old_trend
        if acol_prev <= self.threshold and acol > self.threshold and old_trend is not True:
            trend = True
        if acol_prev >= -self.threshold and acol < -self.threshold and old_trend is True:
            trend = False
        trend_changed = trend != old_trend
        self._trend_state[symbol] = trend

        pos = self._open_positions.get(symbol)
        side = pos["side"] if pos else None
        if pos is not None:
            bars = self._bars_held(pos, signal_open_time_ms)
            if bars >= self.max_trade_bars:
                await self._close_position(symbol, pos, close, "TIME", f"bars_held={bars}")
                return
            pos["trail_distance"] = self._adaptive_trail(acol, atr)
            if not trend_changed:
                cut = False
                if bars >= self.min_hold_bars:
                    if side == "LONG" and (acol < -self.threshold or close < poc * (1 - self.poc_filter_pct)):
                        await self._close_position(symbol, pos, close, "CUT")
                        cut = True
                    elif side == "SHORT" and (acol > self.threshold or close > poc * (1 + self.poc_filter_pct)):
                        await self._close_position(symbol, pos, close, "CUT")
                        cut = True
                if not cut:
                    await self._manage_existing_position(symbol, pos, close, high, low, signal_open_time_ms)
            elif trend is not None:
                can_enter = self._entry_filter(bool(trend), close, poc)
                if can_enter:
                    await self._close_position(symbol, pos, close, "REV")
                    await self._open_new_position(symbol, bool(trend), close, atr, poc, acol, acol_prev, signal_open_time_ms)
        elif trend_changed and trend is not None and self._entry_filter(bool(trend), close, poc):
            if len(self._open_positions) < self.max_concurrent_positions:
                await self._open_new_position(symbol, bool(trend), close, atr, poc, acol, acol_prev, signal_open_time_ms)

    def _entry_filter(self, trend: bool, close: float, poc: float) -> bool:
        return (trend and close > poc * (1 + self.poc_filter_pct)) or (
            not trend and close < poc * (1 - self.poc_filter_pct)
        )

    def _side_for_trend(self, trend: bool) -> str:
        side = "LONG" if trend else "SHORT"
        if self.reverse_side:
            return "SHORT" if side == "LONG" else "LONG"
        return side

    async def _open_new_position(
        self,
        symbol: str,
        trend: bool,
        close: float,
        atr: float,
        poc: float,
        acol: float,
        acol_prev: float,
        signal_open_time_ms: int,
    ) -> None:
        if not self.ctx.can_open_trades():
            return
        side = self._side_for_trend(trend)
        trail_dist = self._adaptive_trail(acol, atr)
        tp_dist = trail_dist * self.tp_ratio
        entry = close
        if side == "LONG":
            sl = entry - trail_dist
            tp = entry + tp_dist
        else:
            sl = entry + trail_dist
            tp = entry - tp_dist
        if self.initial_fixed_tp_sl_pct is not None:
            pct = self.initial_fixed_tp_sl_pct
            sl = entry * (1 - pct if side == "LONG" else 1 + pct)
            tp = entry * (1 + pct if side == "LONG" else 1 - pct)
        trade_size = self._kelly_size()
        leverage = self._symbol_leverage(symbol)
        qty = trade_size / entry
        position_id = f"{self.alpha_id}:{symbol}:{side}:{signal_open_time_ms}"
        self._open_positions[symbol] = {
            "position_id": position_id,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "trail_distance": trail_dist,
            "hse": entry,
            "lse": entry,
            "size": trade_size,
            "entry_candle_open_ms": signal_open_time_ms,
            "signal_candle_close_ms": signal_open_time_ms + _tf_ms(self.tf),
        }
        await self.ctx.emit_signal(
            "OPEN",
            symbol=symbol,
            side=side,
            tf=self.tf,
            entry=entry,
            qty=qty,
            tp=tp,
            sl=sl,
            leverage=leverage,
            position_id=position_id,
            exchange=self.exchange,
            fee_pct=self.fee_pct,
            metadata=json.dumps({
                "atr": round(atr, 6),
                "poc": round(poc, 6),
                "trail_distance": round(trail_dist, 6),
                "trail_mult": round(trail_dist / atr, 3) if atr else None,
                "trend": "BULL" if trend else "BEAR",
                "reverse_side": self.reverse_side,
                "trade_size": round(trade_size, 2),
                "leverage": leverage,
                "margin": round(trade_size / leverage, 2) if leverage else None,
                "cur_equity": round(self._cur_eq, 2),
                "kelly_trades": len(self._recent_results),
                "acol": acol,
                "acol_prev": acol_prev,
            }),
            signal_candle_open_ms=signal_open_time_ms,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def _manage_existing_position(
        self,
        symbol: str,
        pos: dict,
        close: float,
        high: float,
        low: float,
        current_time_ms: int,
    ) -> None:
        if current_time_ms and self._bars_held(pos, current_time_ms) < self.min_hold_bars:
            return
        side = pos["side"]
        trail_dist = float(pos["trail_distance"])
        if side == "LONG":
            pos["hse"] = max(float(pos["hse"]), high)
            new_sl = pos["hse"] - trail_dist
            if new_sl > pos["sl"]:
                pos["sl"] = new_sl
                await self.ctx.emit_signal("MODIFY", symbol=symbol, tf=self.tf, position_id=pos["position_id"], sl=new_sl)
            if low <= pos["sl"]:
                await self._close_position(symbol, pos, low, "SL")
                return
            if high >= pos["tp"]:
                await self._close_position(symbol, pos, pos["tp"], "TP")
        else:
            pos["lse"] = min(float(pos["lse"]), low)
            new_sl = pos["lse"] + trail_dist
            if new_sl < pos["sl"]:
                pos["sl"] = new_sl
                await self.ctx.emit_signal("MODIFY", symbol=symbol, tf=self.tf, position_id=pos["position_id"], sl=new_sl)
            if high >= pos["sl"]:
                await self._close_position(symbol, pos, high, "SL")
                return
            if low <= pos["tp"]:
                await self._close_position(symbol, pos, pos["tp"], "TP")

    async def _close_position(self, symbol: str, pos: dict, exit_price: float, reason: str, detail: str = "") -> None:
        await self.ctx.emit_signal(
            "CLOSE",
            symbol=symbol,
            tf=self.tf,
            position_id=pos["position_id"],
            exit_price=exit_price,
            reason=reason,
            metadata=json.dumps({"reason": reason, "detail": detail}) if detail else None,
            signal_candle_open_ms=pos.get("entry_candle_open_ms", ""),
        )
        self._open_positions.pop(symbol, None)
        trade_size = float(pos.get("size", self._cur_size))
        net = _calc_net_pnl(pos["side"], pos["entry"], exit_price, trade_size, self.fee_pct)
        self._recent_results.append(net > 0)
        if len(self._recent_results) > self.kelly_lookback * 2:
            self._recent_results = self._recent_results[-self.kelly_lookback * 2:]
        self._cur_eq += net
        self._cur_size += self.scale_factor * net
        max_size = self.scale_factor * self._cur_eq
        self._cur_size = max(self.min_invest, min(self._cur_size, max_size))
