"""M15 bangoc execution engine with an H1 Indi1-dot admission gate."""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from .config import settings
from .strategy import (
    BangocIndicators,
    compute_bangoc_dot_color,
    compute_bangoc_indicators,
    get_candle_seconds,
    is_m15_signal_allowed_by_h1_dot,
)
from base.engine import BaseEngine

logger = logging.getLogger(__name__)


class BangocV22Engine(BaseEngine):
    """Preserves bangoc's M15 rules while H1 Indi1 decides entry eligibility."""

    def __init__(self) -> None:
        super().__init__(settings)
        self._open_positions: dict[str, dict[str, Any]] = {}
        self._columns_config_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")
        self._last_processed_m15_open_ms = 0
        self._runner_entry_gate: Callable[[], bool] = lambda: True

    def get_required_channels(self) -> list[str]:
        return [f"kline:{settings.TF}", f"kline:{settings.HTF}"]

    def _get_warmup_symbols(self) -> list[str]:
        return [] if self._is_blacklisted(settings.SYMBOL) else [settings.SYMBOL]

    def _has_open_positions(self) -> bool:
        return bool(self._open_positions)

    async def _manage_positions(self) -> None:
        return

    async def on_warmup_complete(self) -> None:
        if self._columns_config_path:
            self.load_columns_config(self._columns_config_path)

    def set_runner_entry_gate(self, gate: Callable[[], bool]) -> None:
        """Uses the runner's stale-data safety gate before opening a paper position."""
        self._runner_entry_gate = gate

    async def scan_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self._wait_until_next_candle_offset()
                if self.shutdown_event.is_set():
                    break
                await self._process_symbol()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scan error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    async def _wait_until_next_candle_offset(self) -> None:
        candle_len = get_candle_seconds(settings.TF)
        now = time.time()
        next_candle = (int(now // candle_len) + 1) * candle_len
        target = next_candle + settings.OFFSET_CANDLE_SEC
        wait = target - now
        if wait > 0:
            await asyncio.sleep(wait)

    async def _process_symbol(self) -> None:
        async with self.data_lock:
            m15_data = self.symbol_data.get(settings.SYMBOL, {}).get(settings.TF)
            h1_data = self.symbol_data.get(settings.SYMBOL, {}).get(settings.HTF)
            if not m15_data or not m15_data.price_list or not h1_data or not h1_data.price_list:
                return
            m15_close_list = list(m15_data.price_list)
            h1_close_list = list(h1_data.price_list)
            signal_open_time_ms = m15_data.time_list[-1] if m15_data.time_list else 0
            h1_open_time_ms = h1_data.time_list[-1] if h1_data.time_list else 0

        if signal_open_time_ms <= self._last_processed_m15_open_ms:
            return
        h1_candle_ms = get_candle_seconds(settings.HTF) * 1000
        required_h1_open_ms = signal_open_time_ms - signal_open_time_ms % h1_candle_ms
        if h1_open_time_ms < required_h1_open_ms:
            return
        if not self._runner_entry_gate():
            logger.warning("[RUNNER-GATE] reject M15 %s because runner is not entry-ready", signal_open_time_ms)
            return

        indic = compute_bangoc_indicators(m15_close_list)
        self._last_processed_m15_open_ms = signal_open_time_ms
        if indic is None or indic.side is None:
            return

        h1_dot_green = compute_bangoc_dot_color(h1_close_list)
        if h1_dot_green is None or not is_m15_signal_allowed_by_h1_dot(indic.side, h1_dot_green):
            logger.info(
                "[H1-GATE] reject M15 %s because H1 Indi1=%s",
                indic.side,
                self._color(h1_dot_green),
            )
            return

        symbol = settings.SYMBOL
        pos = self._open_positions.get(symbol)
        if pos and not self._claim_position_candle(pos, signal_open_time_ms):
            return
        if pos and pos["side"] == indic.side:
            return

        if pos:
            self._close_position(symbol, pos, indic, h1_dot_green, "REV")
            if self.can_open_new_trades():
                self._open_new_position(symbol, indic, h1_dot_green, signal_open_time_ms)
            return

        if self.can_open_new_trades() and len(self._open_positions) < settings.MAX_CONCURRENT_POSITIONS:
            self._open_new_position(symbol, indic, h1_dot_green, signal_open_time_ms)

    def _open_new_position(
        self,
        symbol: str,
        indic: BangocIndicators,
        h1_dot_green: bool,
        signal_open_time_ms: int,
    ) -> None:
        entry = indic.close
        qty = settings.INVEST_PER_TRADE / entry
        position_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        candle_close_ms = signal_open_time_ms + get_candle_seconds(settings.TF) * 1000

        self._open_positions[symbol] = {
            "position_id": position_id,
            "side": indic.side,
            "entry": entry,
            "size": settings.INVEST_PER_TRADE,
            "entry_candle_open_ms": signal_open_time_ms,
            "signal_candle_close_ms": candle_close_ms,
        }
        self.mark_positions_changed()

        self.push_signal(
            "OPEN",
            symbol=symbol,
            side=indic.side,
            entry=entry,
            qty=qty,
            leverage=settings.LEVERAGE,
            position_id=position_id,
            exchange=settings.EXCHANGE,
            fee_pct=settings.FEE_PCT,
            metadata=json.dumps(self._metadata(indic, h1_dot_green)),
            timestamp=timestamp,
        )
        logger.info(
            "[OPEN] %s %s @ %.6f | M15 indi1=%s indi2=%s | H1 indi1=%s",
            indic.side,
            symbol,
            entry,
            self._color(indic.indi1_green),
            self._color(indic.indi2_green),
            self._color(h1_dot_green),
        )

    def _close_position(
        self,
        symbol: str,
        pos: dict[str, Any],
        indic: BangocIndicators,
        h1_dot_green: bool,
        reason: str,
    ) -> None:
        metadata = self._metadata(indic, h1_dot_green)
        metadata["close_model"] = "m15_signal_reversal_after_h1_dot_gate"
        metadata["reason"] = reason

        self.push_signal(
            "CLOSE",
            position_id=pos["position_id"],
            exit_price=indic.close,
            reason=reason,
            metadata=json.dumps(metadata),
        )
        self._open_positions.pop(symbol, None)
        self.mark_positions_changed()

        logger.info(
            "[CLOSE] %s %s reason=%s @ %.6f -> %s",
            pos["side"],
            symbol,
            reason,
            indic.close,
            indic.side,
        )

    @staticmethod
    def _color(is_green: bool | None) -> str:
        return "GREEN" if is_green else "RED" if is_green is False else "UNREADY"

    def _metadata(self, indic: BangocIndicators, h1_dot_green: bool) -> dict[str, Any]:
        return {
            "tf": settings.TF,
            "rule": "m15_indi1_and_indi2_with_matching_h1_indi1",
            "indi1_color": self._color(indic.indi1_green),
            "indi1_acol": round(indic.indi1_acol, 6),
            "indi1_acol_prev": round(indic.indi1_acol_prev, 6),
            "indi2_color": self._color(indic.indi2_green),
            "indi2_poc": round(indic.indi2_poc, 6),
            "indi2_lower": round(indic.indi2_lower, 6),
            "indi2_upper": round(indic.indi2_upper, 6),
            "h1_dot_tf": settings.HTF,
            "h1_dot_color": self._color(h1_dot_green),
        }
