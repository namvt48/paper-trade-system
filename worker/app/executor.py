import json
import uuid
import logging
import inspect
from datetime import datetime, timezone
from app.models import OpenSignal, ModifySignal, CloseSignal, RegisterColumnsSignal, SignalType
from app.fill import fixed_pct_fill
from app.ob_exec import PriceQuote

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, db, slippage_pct: float = 0.05, duplicate_policy: str = "reject"):
        self.db = db
        self.slippage_pct = slippage_pct
        self.duplicate_policy = duplicate_policy

    async def process_open(self, signal: OpenSignal, fill_price: float | None = None) -> dict:
        await self.db.register_alpha(signal.alpha_id)

        existing = await self.db.get_open_position_by_alpha_symbol(
            signal.alpha_id, signal.symbol
        )
        if existing:
            if self.duplicate_policy == "reject":
                raise ValueError(
                    f"Alpha {signal.alpha_id} already has an open position on {signal.symbol}"
                )

        fill_price = (
            fill_price if fill_price is not None
            else self._apply_slippage(signal.entry, signal.side)
        )
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

    @staticmethod
    def close_ref_price(signal: CloseSignal, pos) -> float:
        """Reference exit price for a close: the signal's exit_price, else the entry price.
        Shared with main.process_signal_message so the RPC ref and the recorded close
        price are always derived the same way."""
        return signal.exit_price or pos["entry_price"]

    @staticmethod
    def close_ref_is_executable(signal: CloseSignal) -> bool:
        try:
            metadata = json.loads(signal.metadata or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        return bool(metadata.get("ref_is_executable")) if isinstance(metadata, dict) else False

    async def process_close(self, signal: CloseSignal, fill_price: float | None = None) -> dict:
        pos = await self.db.get_position(signal.position_id)
        if not pos:
            raise ValueError(f"Position not found: {signal.position_id}")

        close_qty = pos["qty"] if signal.qty is None else signal.qty
        if close_qty <= 0:
            raise ValueError(f"Close qty must be positive: {close_qty}")
        if close_qty > pos["qty"] + 1e-12:
            raise ValueError(
                f"Close qty exceeds open qty: {close_qty} > {pos['qty']}"
            )

        raw_exit = self.close_ref_price(signal, pos)
        exit_price = (
            fill_price if fill_price is not None
            else raw_exit if self.close_ref_is_executable(signal)
            else self._apply_slippage(raw_exit, pos["side"], is_close=True)
        )

        close_metadata = None
        if signal.metadata and signal.metadata not in ("{}", ""):
            try:
                close_meta = json.loads(signal.metadata)
                if isinstance(close_meta, dict):
                    close_meta["fill_price"] = exit_price
                    close_metadata = json.dumps(close_meta)
            except (json.JSONDecodeError, TypeError):
                pass

        await self.db.close_position(
            position_id=signal.position_id,
            exit_price=exit_price,
            reason=signal.reason,
            closed_at=signal.timestamp,
            close_metadata=close_metadata,
            qty=close_qty,
        )

        remaining_qty = max(pos["qty"] - close_qty, 0.0)
        fully_closed = remaining_qty <= 1e-12
        logger.info(
            "[CLOSE] %s %s reason=%s qty=%.8f remaining=%.8f raw_fill=%.6f fill=%.6f",
            signal.alpha_id, signal.position_id, signal.reason, close_qty,
            remaining_qty, raw_exit, exit_price,
        )
        return {
            "position_id": signal.position_id,
            "exit_price": exit_price,
            "closed": fully_closed,
            "closed_qty": close_qty,
            "remaining_qty": remaining_qty,
        }

    async def process_register_columns(self, signal: RegisterColumnsSignal) -> dict:
        import json
        try:
            columns = json.loads(signal.columns)
        except (json.JSONDecodeError, TypeError):
            raise ValueError(f"Invalid columns JSON: {signal.columns}")

        if not isinstance(columns, list):
            raise ValueError("columns must be a JSON array")

        await self.db.register_alpha(signal.alpha_id)
        await self.db.register_alpha_columns(signal.alpha_id, columns)
        return {"alpha_id": signal.alpha_id, "columns_registered": len(columns)}

    @staticmethod
    def _as_price_fn(price_source):
        if callable(price_source):
            return price_source
        return lambda symbol, side: price_source.get(symbol)

    async def check_tpsl_hits(self, price_source, fill_resolver=None) -> list[dict]:
        price_fn = self._as_price_fn(price_source)
        positions = await self.db.get_positions_with_tpsl()
        hits = []

        for pos in positions:
            raw_quote = price_fn(pos["symbol"], pos["side"])
            if raw_quote is None:
                continue
            quote = (
                raw_quote if isinstance(raw_quote, PriceQuote)
                else PriceQuote(float(raw_quote), "legacy", False)
            )
            current_price = quote.price

            closed = False
            reason = None
            exit_price = None

            stop_price = None
            if pos["side"] == "LONG":
                if pos["tp"] is not None and current_price >= pos["tp"]:
                    closed = True
                    reason = "TP_HIT"
                    stop_price = pos["tp"]
                    exit_price = current_price
                elif pos["sl"] is not None and current_price <= pos["sl"]:
                    closed = True
                    reason = "SL_HIT"
                    stop_price = pos["sl"]
                    exit_price = current_price
            elif pos["side"] == "SHORT":
                if pos["tp"] is not None and current_price <= pos["tp"]:
                    closed = True
                    reason = "TP_HIT"
                    stop_price = pos["tp"]
                    exit_price = current_price
                elif pos["sl"] is not None and current_price >= pos["sl"]:
                    closed = True
                    reason = "SL_HIT"
                    stop_price = pos["sl"]
                    exit_price = current_price

            if closed and exit_price is not None:
                if fill_resolver is None:
                    fill_exit = self._apply_slippage(exit_price, pos["side"], is_close=True)
                else:
                    try:
                        parameters = inspect.signature(fill_resolver).parameters
                        if "ref_is_executable" in parameters or len(parameters) >= 7:
                            fill_exit = await fill_resolver(
                                pos.get("exchange", "binance"), pos["symbol"], pos["side"],
                                pos["qty"], exit_price, True, quote.is_executable,
                            )
                        else:
                            fill_exit = await fill_resolver(
                                pos.get("exchange", "binance"), pos["symbol"], pos["side"],
                                pos["qty"], exit_price, True,
                            )
                    except Exception as exc:
                        # One symbol's fill failure must not abort the whole auto-close
                        # pass; fall back to fixed-pct for this position.
                        logger.warning("[TPSL] fill_resolver failed for %s: %s; using fixed-pct",
                                       pos["symbol"], exc)
                        fill_exit = self._apply_slippage(exit_price, pos["side"], is_close=True)
                now = datetime.now(timezone.utc).isoformat()
                close_meta = json.dumps({
                    "close_model": "worker_tpsl_auto",
                    "reason": reason,
                    "stop_price": stop_price,
                    "trigger_price": current_price,
                    "raw_fill_price": exit_price,
                    "fill_price": fill_exit,
                    "fill_reference_source": quote.source,
                    "ref_is_executable": quote.is_executable,
                })
                await self.db.close_position(
                    position_id=pos["position_id"],
                    exit_price=fill_exit,
                    reason=reason,
                    closed_at=now,
                    close_metadata=close_meta,
                )
                hits.append({"position_id": pos["position_id"], "reason": reason, "exit_price": fill_exit})
                logger.info(
                    "[%s] %s %s %s stop=%.6f trigger=%.6f fill=%.6f",
                    reason, pos["alpha_id"], pos["side"], pos["symbol"],
                    stop_price, current_price, fill_exit,
                )

        return hits

    async def check_tpsl_hit(self, symbol: str, price: float) -> list[dict]:
        return await self.check_tpsl_hits({symbol: price})

    def _apply_slippage(self, price: float, side: str, is_close: bool = False) -> float:
        return fixed_pct_fill(price, side, self.slippage_pct, is_close)
