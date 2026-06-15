import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.strategy import BangocIndicators, compute_bangoc_indicators, get_candle_seconds
from base.engine import BaseEngine

logger = logging.getLogger(__name__)


class Alpha1BangocEngine(BaseEngine):
    def __init__(self):
        super().__init__(settings)
        self._open_positions: dict[str, dict] = {}
        self._columns_config_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")

    def get_required_channels(self) -> list[str]:
        return [f"kline:{settings.TF}"]

    def _get_warmup_symbols(self) -> list[str]:
        return [] if self._is_blacklisted(settings.SYMBOL) else [settings.SYMBOL]

    def _has_open_positions(self) -> bool:
        return bool(self._open_positions)

    async def _manage_positions(self) -> None:
        return

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
            sd = self.symbol_data.get(settings.SYMBOL, {}).get(settings.TF)
            if not sd or not sd.price_list:
                return
            close_list = list(sd.price_list)
            signal_open_time_ms = sd.time_list[-1] if sd.time_list else 0

        indic = compute_bangoc_indicators(close_list)
        if indic is None or indic.side is None:
            return

        symbol = settings.SYMBOL
        pos = self._open_positions.get(symbol)
        if pos and not self._claim_position_candle(pos, signal_open_time_ms):
            return
        if pos and pos["side"] == indic.side:
            return

        if pos:
            self._close_position(symbol, pos, indic, "REV")
            if self.can_open_new_trades():
                self._open_new_position(symbol, indic, signal_open_time_ms)
            return

        if self.can_open_new_trades() and len(self._open_positions) < settings.MAX_CONCURRENT_POSITIONS:
            self._open_new_position(symbol, indic, signal_open_time_ms)

    def _open_new_position(self, symbol: str, indic: BangocIndicators, signal_open_time_ms: int) -> None:
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
            metadata=json.dumps(self._metadata(indic)),
            timestamp=timestamp,
        )
        logger.info(
            "[OPEN] %s %s @ %.6f | indi1=%s acol=%.4f indi2=%s poc=%.6f",
            indic.side,
            symbol,
            entry,
            self._color(indic.indi1_green),
            indic.indi1_acol,
            self._color(indic.indi2_green),
            indic.indi2_poc,
        )

    def _close_position(self, symbol: str, pos: dict, indic: BangocIndicators, reason: str) -> None:
        metadata = self._metadata(indic)
        metadata["close_model"] = "m15_signal_reversal"
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
    def _color(is_green: bool) -> str:
        return "GREEN" if is_green else "RED"

    def _metadata(self, indic: BangocIndicators) -> dict:
        return {
            "tf": settings.TF,
            "rule": "indi1_and_indi2_same_color",
            "indi1_color": self._color(indic.indi1_green),
            "indi1_acol": round(indic.indi1_acol, 6),
            "indi1_acol_prev": round(indic.indi1_acol_prev, 6),
            "indi2_color": self._color(indic.indi2_green),
            "indi2_poc": round(indic.indi2_poc, 6),
            "indi2_lower": round(indic.indi2_lower, 6),
            "indi2_upper": round(indic.indi2_upper, 6),
        }
