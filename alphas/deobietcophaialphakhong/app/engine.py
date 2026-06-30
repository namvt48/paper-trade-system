"""DeoBietCoPhaiAlphaKhong — Song Than zone-based mean-reversion engine.

Implements docs/alphas/song_than.md as a legacy_standalone BaseEngine:

Pipeline per 15m bar close:
  1. Resample not needed — ZONE_TF == TF (15m).
  2. Compute swing points (section 2.1) over the full candle buffer.
  3. Compute trailing levels (section 2.2).
  4. Compute zones from the latest trailing pair (section 2.3).
  5. Emit LIMIT entry signals at zone edges (section 3).
  6. On the *next* bar, check fills against pending limit orders.
  7. For open positions, check exit conditions in priority order (section 4.3):
     SL → milestone upgrade → TP_TRAIL (milestone >= 2) → TP.
  8. Track consecutive SL count; after REVERSE_SL_COUNT, flip side via MARKET
     (section 5).

Sizing: margin = INVEST_PER_TRADE ($100) x LEVERAGE (50) = $5000 notional.
        qty = notional / entry_price.
"""
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import (
    NaN,
    compute_swing_points,
    compute_trailing_levels,
    compute_zones,
    get_entry_signals,
    get_trailing_milestone,
    update_trailing_sl,
)
from base.engine import BaseEngine

logger = logging.getLogger(__name__)


def _candle_seconds(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    return 3600


def _calc_net_pnl(side: str, entry: float, exit_price: float, size: float, fee_rate: float) -> float:
    qty = size / entry
    gross = qty * (exit_price - entry) if side == "LONG" else qty * (entry - exit_price)
    fee_in = fee_rate * size
    fee_out = fee_rate * (qty * exit_price)
    return gross - fee_in - fee_out


class DeoBietCoPhaiAlphaKhongEngine(BaseEngine):
    """Song Than zone-based mean-reversion paper-trade engine.

    State per symbol:
      - ``_open_positions[symbol]``    : active position dict (or absent).
      - ``_pending_orders[symbol]``    : list of unfilled LIMIT orders from the
                                          last closed bar (LONG/SHORT at zone edge).
      - ``_consecutive_sl``            : consecutive same-side SL counter.
      - ``_last_sl_side``              : "LONG" | "SHORT" | None — drives reverse.
      - ``_last_zones[symbol]``        : last computed (green_high, red_low) for audit.
      - ``_last_trail[symbol]``        : last (trail_up, trail_dn) for audit.
    """

    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict[str, Any]] = {}
        self._pending_orders: dict[str, list[dict[str, Any]]] = {}
        self._consecutive_sl: int = 0
        self._last_sl_side: Optional[str] = None
        self._last_zones: dict[str, tuple[float, float]] = {}
        self._last_trail: dict[str, tuple[float, float]] = {}
        self._leverage_map: dict[str, int] = {}
        self._load_blacklist_file()
        self._columns_config_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")

    # ── BaseEngine interface ──────────────────────────────────────────────────

    def get_required_channels(self) -> list[str]:
        return [f"kline:{settings.TF}"]

    def _get_warmup_symbols(self) -> list[str]:
        return ["BTCUSDT"]

    def _has_open_positions(self) -> bool:
        return len(self._open_positions) > 0

    # ── Blacklist ─────────────────────────────────────────────────────────────

    def _load_blacklist_file(self) -> None:
        path = settings.BLACKLIST_FILE
        if not path:
            return
        if not os.path.isfile(path):
            logger.warning("[%s] BLACKLIST_FILE not found: %s", settings.ALPHA_ID, path)
            return
        loaded: set[str] = set()
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                loaded.add(line.upper())
        self._blacklist |= loaded
        logger.info(
            "[%s] Blacklist loaded: %d symbols (total: %d)",
            settings.ALPHA_ID, len(loaded), len(self._blacklist),
        )

    def _get_symbol_leverage(self, symbol: str) -> int:
        return self._leverage_map.get(symbol, settings.LEVERAGE)

    # ── Sizing ────────────────────────────────────────────────────────────────

    def _trade_size(self) -> float:
        """Notional = margin × leverage. Default $100 × 50 = $5000."""
        return float(settings.INVEST_PER_TRADE) * int(settings.LEVERAGE)

    # ── Per-symbol row for legacy_standalone parallel scan path ───────────────

    def _build_symbol_row(self, symbol: str) -> dict[str, Any] | None:
        tf_map = self.symbol_data.get(symbol)
        if not tf_map:
            return None
        sd = tf_map.get(settings.TF)
        if not sd or not sd.price_list or not sd.high_list or not sd.low_list:
            return None
        return {
            "symbol": symbol,
            "close_list": sd.price_list,
            "high_list": sd.high_list,
            "low_list": sd.low_list,
            "open_list": sd.open_list,
            "time_list": sd.time_list,
            "signal_open_time_ms": sd.time_list[-1] if sd.time_list else 0,
        }

    def _compute_indicators(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Compute swing points → trailing levels → zones for the full buffer.

        Returns the latest trail pair + zones, or ``None`` if insufficient data.
        """
        high = row["high_list"]
        low = row["low_list"]
        L = settings.SWING_LENGTH
        if len(high) <= L + 1:
            return None

        swing_high, swing_low = compute_swing_points(high, low, L=L)
        trail_up, trail_dn = compute_trailing_levels(swing_high, swing_low, high, low, L=L)

        i = len(high) - 1
        tu = trail_up[i]
        td = trail_dn[i]
        if isinstance(tu, float) and isinstance(td, float):
            zones = compute_zones(tu, td)
        else:
            zones = None
        return {
            "trail_up": tu,
            "trail_dn": td,
            "zones": zones,
            "latest_close": row["close_list"][-1],
            "latest_high": high[-1],
            "latest_low": low[-1],
            "signal_open_time_ms": row.get("signal_open_time_ms", 0),
        }

    def _apply_decision(self, row: dict[str, Any], indic: dict[str, Any]) -> None:
        """Apply trading decisions — mutates state. Must run on event loop."""
        symbol = row["symbol"]
        signal_open_time_ms: int = int(indic.get("signal_open_time_ms", 0))
        zones = indic.get("zones")
        trail_up = indic.get("trail_up")
        trail_dn = indic.get("trail_dn")
        latest_high: float = float(indic.get("latest_high", 0.0))
        latest_low: float = float(indic.get("latest_low", 0.0))
        latest_close: float = float(indic.get("latest_close", 0.0))

        if zones is not None:
            green_low, green_high, red_low, red_high = zones
            self._last_zones[symbol] = (green_high, red_low)
            if isinstance(trail_up, float) and isinstance(trail_dn, float):
                self._last_trail[symbol] = (trail_up, trail_dn)
        else:
            # Zones invalid this bar — clear any pending limits (no edge to rest at).
            self._pending_orders.pop(symbol, None)
            return

        # ── 1. Reverse entry check (section 5) ────────────────────────────────
        if (
            self._consecutive_sl >= settings.REVERSE_SL_COUNT
            and symbol not in self._open_positions
            and not self._pending_orders.get(symbol)
            and self.can_open_new_trades()
        ):
            reverse_side = "LONG" if self._last_sl_side == "SHORT" else "SHORT"
            self._open_market_position(symbol, reverse_side, latest_close, signal_open_time_ms, is_reverse=True)
            self._consecutive_sl = 0
            self._last_sl_side = None
            return

        # ── 2. Manage existing position (section 4) ───────────────────────────
        pos = self._open_positions.get(symbol)
        if pos is not None:
            self._manage_position(symbol, pos, latest_high, latest_low, latest_close, signal_open_time_ms)
            # If still open after management, do not place new limits this bar.
            if symbol in self._open_positions:
                self._pending_orders.pop(symbol, None)
                return

        # ── 3. Check pending limit fills (section 3 — fill on this bar) ───────
        pending = self._pending_orders.get(symbol, [])
        still_pending: list[dict[str, Any]] = []
        filled = False
        for order in pending:
            side = order["side"]
            limit_price = order["limit_price"]
            if side == "LONG" and latest_low <= limit_price:
                self._open_limit_position(symbol, "LONG", limit_price, signal_open_time_ms)
                filled = True
                break
            if side == "SHORT" and latest_high >= limit_price:
                self._open_limit_position(symbol, "SHORT", limit_price, signal_open_time_ms)
                filled = True
                break
            still_pending.append(order)
        if filled:
            self._pending_orders.pop(symbol, None)
            return
        if not still_pending:
            self._pending_orders.pop(symbol, None)

        # ── 4. Emit new LIMIT entry signals from this closed bar (section 3) ─
        # Only if no position and no surviving pending order for this symbol.
        if symbol not in self._open_positions and not self._pending_orders.get(symbol):
            if self.can_open_new_trades() and len(self._open_positions) < settings.MAX_CONCURRENT_POSITIONS:
                signals = get_entry_signals(
                    bar_low=latest_low,
                    bar_high=latest_high,
                    green_high=green_high,
                    red_low=red_low,
                )
                if signals:
                    self._pending_orders[symbol] = [
                        {"side": s["side"], "limit_price": s["limit_price"]}
                        for s in signals
                    ]

    # ── Position management (section 4.3) ─────────────────────────────────────

    def _manage_position(
        self,
        symbol: str,
        pos: dict[str, Any],
        bar_high: float,
        bar_low: float,
        bar_close: float,
        signal_open_time_ms: int,
    ) -> None:
        side = pos["side"]
        sl = pos["sl"]
        tp = pos["tp"]

        # Priority 1: SL hit
        if side == "LONG" and bar_low <= sl:
            self._close_position(symbol, pos, sl, "SL", f"low={bar_low:.6f} <= sl={sl:.6f}")
            self._record_sl(side)
            return
        if side == "SHORT" and bar_high >= sl:
            self._close_position(symbol, pos, sl, "SL", f"high={bar_high:.6f} >= sl={sl:.6f}")
            self._record_sl(side)
            return

        # Priority 2: milestone upgrade → tighten SL (never loosens)
        entry = pos["entry"]
        milestone = pos.get("milestone", 0)
        new_milestone = get_trailing_milestone(
            entry_price=entry,
            side=side,
            bar_high=bar_high,
            bar_low=bar_low,
            m1_pct=settings.TRAIL_M1_PCT,
            m2_pct=settings.TRAIL_M2_PCT,
        )
        if new_milestone > milestone:
            pos["milestone"] = new_milestone
            new_sl = update_trailing_sl(
                entry_price=entry,
                side=side,
                milestone=new_milestone,
                m1_sl_pct=settings.TRAIL_M1_SL_PCT,
                m2_sl_pct=settings.TRAIL_M2_SL_PCT,
            )
            if new_sl is not None:
                if side == "LONG" and new_sl > sl:
                    pos["sl"] = new_sl
                    self.push_signal("MODIFY", position_id=pos["position_id"], sl=new_sl)
                elif side == "SHORT" and new_sl < sl:
                    pos["sl"] = new_sl
                    self.push_signal("MODIFY", position_id=pos["position_id"], sl=new_sl)

        # Priority 3: TP_TRAIL — milestone >= 2 → close at close
        if pos.get("milestone", 0) >= 2:
            self._close_position(symbol, pos, bar_close, "TP_TRAIL", f"milestone>=2 close={bar_close:.6f}")
            return

        # Priority 4: TP hit
        if side == "LONG" and bar_high >= tp:
            self._close_position(symbol, pos, tp, "TP", f"high={bar_high:.6f} >= tp={tp:.6f}")
            return
        if side == "SHORT" and bar_low <= tp:
            self._close_position(symbol, pos, tp, "TP", f"low={bar_low:.6f} <= tp={tp:.6f}")
            return

    def _record_sl(self, side: str) -> None:
        """Update consecutive SL counter (section 5)."""
        if side == self._last_sl_side:
            self._consecutive_sl += 1
        else:
            self._consecutive_sl = 1
            self._last_sl_side = side

    # ── Position open/close ───────────────────────────────────────────────────

    def _open_limit_position(
        self,
        symbol: str,
        side: str,
        entry: float,
        signal_open_time_ms: int,
    ) -> None:
        self._open_position(symbol, side, entry, signal_open_time_ms, is_reverse=False)

    def _open_market_position(
        self,
        symbol: str,
        side: str,
        entry: float,
        signal_open_time_ms: int,
        is_reverse: bool = True,
    ) -> None:
        self._open_position(symbol, side, entry, signal_open_time_ms, is_reverse=is_reverse)

    def _open_position(
        self,
        symbol: str,
        side: str,
        entry: float,
        signal_open_time_ms: int,
        is_reverse: bool,
    ) -> None:
        if not self.can_open_new_trades():
            return
        if len(self._open_positions) >= settings.MAX_CONCURRENT_POSITIONS:
            return

        if is_reverse:
            sl_pct = settings.REVERSE_SL_PCT
            tp_pct = settings.REVERSE_TP_PCT
        else:
            sl_pct = settings.SL_LONG_PCT if side == "LONG" else settings.SL_SHORT_PCT
            tp_pct = settings.TP_PCT

        if side == "LONG":
            sl = entry * (1 - sl_pct)
            tp = entry * (1 + tp_pct)
        else:
            sl = entry * (1 + sl_pct)
            tp = entry * (1 - tp_pct)

        size = self._trade_size()
        qty = size / entry
        symbol_lev = self._get_symbol_leverage(symbol)
        position_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        signal_candle_close_ms = signal_open_time_ms + _candle_seconds(settings.TF) * 1000

        self._open_positions[symbol] = {
            "position_id": position_id,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "size": size,
            "milestone": 0,
            "is_reverse": is_reverse,
            "entry_candle_open_ms": signal_open_time_ms,
            "signal_candle_close_ms": signal_candle_close_ms,
        }
        self.mark_positions_changed()

        self.push_signal(
            "OPEN",
            symbol=symbol,
            side=side,
            entry=entry,
            qty=qty,
            tp=tp,
            sl=sl,
            leverage=symbol_lev,
            position_id=position_id,
            exchange=settings.EXCHANGE,
            fee_pct=settings.FEE_PCT,
            metadata=json.dumps({
                "milestone": 0,
                "is_reverse": is_reverse,
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
                "trade_size": round(size, 2),
                "leverage": symbol_lev,
                "margin": round(size / symbol_lev, 2),
            }),
            timestamp=timestamp,
        )
        logger.info(
            "[OPEN] %s %s @ %.6f sl=%.6f tp=%.6f size=%.2f lev=%dx rev=%s | cons_sl=%d last_sl=%s",
            side, symbol, entry, sl, tp, size, symbol_lev, is_reverse,
            self._consecutive_sl, self._last_sl_side,
        )

    def _close_position(
        self,
        symbol: str,
        pos: dict[str, Any],
        exit_price: float,
        reason: str,
        detail: str = "",
    ) -> None:
        self.push_signal(
            "CLOSE",
            position_id=pos["position_id"],
            exit_price=exit_price,
            reason=reason,
            metadata=json.dumps({
                "close_detail": detail,
                "milestone": pos.get("milestone", 0),
                "is_reverse": pos.get("is_reverse", False),
            }),
        )
        self._open_positions.pop(symbol, None)
        self.mark_positions_changed()

        size = pos.get("size", settings.INVEST_PER_TRADE)
        net = _calc_net_pnl(pos["side"], pos["entry"], exit_price, size, settings.FEE_PCT)
        logger.info(
            "[CLOSE] %s reason=%s @ %.6f net=%.2f milestone=%d rev=%s%s",
            symbol, reason, exit_price, net, pos.get("milestone", 0),
            pos.get("is_reverse", False), f" | {detail}" if detail else "",
        )

    # ── Real-time price alert handler (section 4 — tick-level SL/TP) ───────────

    def on_price_alert_message(self, msg: dict[str, Any]) -> None:
        symbol = msg.get("symbol", "")
        if not symbol:
            return
        pos = self._open_positions.get(symbol)
        if not pos:
            return
        price = self._trigger_price(pos["side"], msg)
        if price is None or price <= 0:
            return
        side = pos["side"]
        sl = pos["sl"]
        tp = pos["tp"]
        if side == "LONG":
            if price <= sl:
                self._close_position(symbol, pos, price, "SL", f"tick={price:.6f}<=sl={sl:.6f}")
                self._record_sl(side)
            elif price >= tp:
                self._close_position(symbol, pos, price, "TP", f"tick={price:.6f}>=tp={tp:.6f}")
        else:
            if price >= sl:
                self._close_position(symbol, pos, price, "SL", f"tick={price:.6f}>=sl={sl:.6f}")
                self._record_sl(side)
            elif price <= tp:
                self._close_position(symbol, pos, price, "TP", f"tick={price:.6f}<=tp={tp:.6f}")

    # ── Legacy scan loops (used when run standalone, not via runner) ──────────

    async def scan_loop(self) -> None:
        if not self._has_open_positions():
            return
        await self._manage_positions()

    async def _manage_positions(self) -> None:
        if not self._open_positions:
            return
        for symbol in list(self._open_positions.keys()):
            tf_map = self.symbol_data.get(symbol)
            if not tf_map:
                continue
            sd = tf_map.get(settings.TF)
            if not sd or not sd.price_list:
                continue
            pos = self._open_positions.get(symbol)
            if not pos:
                continue
            # Use the latest closed bar for management.
            self._manage_position(
                symbol, pos,
                bar_high=sd.high_list[-1],
                bar_low=sd.low_list[-1],
                bar_close=sd.price_list[-1],
                signal_open_time_ms=sd.time_list[-1] if sd.time_list else 0,
            )
