import uuid
import logging
from datetime import datetime, timezone
from app.models import OpenSignal, ModifySignal, CloseSignal, SignalType

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, db, slippage_pct: float = 0.05, duplicate_policy: str = "reject"):
        self.db = db
        self.slippage_pct = slippage_pct
        self.duplicate_policy = duplicate_policy

    async def process_open(self, signal: OpenSignal) -> dict:
        await self.db.register_alpha(signal.alpha_id)

        existing = await self.db.get_open_position_by_alpha_symbol(
            signal.alpha_id, signal.symbol
        )
        if existing:
            if self.duplicate_policy == "reject":
                raise ValueError(
                    f"Alpha {signal.alpha_id} already has an open position on {signal.symbol}"
                )

        fill_price = self._apply_slippage(signal.entry, signal.side)
        position_id = signal.position_id if signal.position_id else str(uuid.uuid4())

        await self.db.create_position(
            position_id=position_id,
            alpha_id=signal.alpha_id,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            entry_price=fill_price,
            qty=signal.qty,
            tp=signal.tp,
            sl=signal.sl,
            leverage=signal.leverage,
            opened_at=signal.timestamp,
            metadata=signal.metadata,
            exchange=signal.exchange,
            fee_pct=signal.fee_pct,
        )

        return {"position_id": position_id, "fill_price": fill_price}

    async def process_modify(self, signal: ModifySignal) -> dict:
        pos = await self.db.get_position(signal.position_id)
        if not pos:
            raise ValueError(f"Position not found: {signal.position_id}")

        if signal.sl is not None and pos["sl"] is not None:
            if pos["side"] == "LONG" and signal.sl < pos["sl"]:
                raise ValueError(
                    f"Trailing SL cannot move against LONG position: {signal.sl} < {pos['sl']}"
                )
            if pos["side"] == "SHORT" and signal.sl > pos["sl"]:
                raise ValueError(
                    f"Trailing SL cannot move against SHORT position: {signal.sl} > {pos['sl']}"
                )

        await self.db.modify_position(
            position_id=signal.position_id,
            tp=signal.tp,
            sl=signal.sl,
        )

        return {"position_id": signal.position_id, "modified": True}

    async def process_close(self, signal: CloseSignal) -> dict:
        pos = await self.db.get_position(signal.position_id)
        if not pos:
            raise ValueError(f"Position not found: {signal.position_id}")

        exit_price = signal.exit_price or pos["entry_price"]

        await self.db.close_position(
            position_id=signal.position_id,
            exit_price=exit_price,
            reason=signal.reason,
            closed_at=signal.timestamp,
        )

        return {"position_id": signal.position_id, "exit_price": exit_price, "closed": True}

    async def check_tpsl_hits(self, prices: dict[str, float]) -> list[dict]:
        positions = await self.db.get_positions_with_tpsl()
        hits = []

        for pos in positions:
            current_price = prices.get(pos["symbol"])
            if current_price is None:
                continue

            closed = False
            reason = None
            exit_price = None

            if pos["side"] == "LONG":
                if pos["tp"] is not None and current_price >= pos["tp"]:
                    closed = True
                    reason = "TP_HIT"
                    exit_price = pos["tp"]
                elif pos["sl"] is not None and current_price <= pos["sl"]:
                    closed = True
                    reason = "SL_HIT"
                    exit_price = pos["sl"]
            elif pos["side"] == "SHORT":
                if pos["tp"] is not None and current_price <= pos["tp"]:
                    closed = True
                    reason = "TP_HIT"
                    exit_price = pos["tp"]
                elif pos["sl"] is not None and current_price >= pos["sl"]:
                    closed = True
                    reason = "SL_HIT"
                    exit_price = pos["sl"]

            if closed and exit_price is not None:
                now = datetime.now(timezone.utc).isoformat()
                await self.db.close_position(
                    position_id=pos["position_id"],
                    exit_price=exit_price,
                    reason=reason,
                    closed_at=now,
                )
                hits.append({"position_id": pos["position_id"], "reason": reason, "exit_price": exit_price})
                logger.info(f"[{reason}] {pos['alpha_id']} {pos['side']} {pos['symbol']} @ {exit_price}")

        return hits

    async def check_tpsl_hit(self, symbol: str, price: float) -> list[dict]:
        return await self.check_tpsl_hits({symbol: price})

    def _apply_slippage(self, price: float, side: str) -> float:
        slippage = price * (self.slippage_pct / 1000.0)
        if side == "LONG":
            return price + slippage
        else:
            return price - slippage
