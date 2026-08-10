from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time as wall_time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from runner.strategy.base import Strategy

from .logic import Alpha21AlmaCross, alpha19_decision


logger = logging.getLogger(__name__)
POSITION_NAMESPACE = uuid.UUID("1fb87cc1-f373-4c42-885c-fea604945b0c")
TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


class Vn30TcbsRunnerStrategy(Strategy):
    """Alpha 19/21 from alphavn30.html, executed on TCBS futures candles.

    `params.reverse=True` mirrors the strategy's traded side (LONG<->SHORT)
    against the original alpha's signal, e.g. for a "-reverse" clone alpha_id.
    """

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        self.preset = int(params.get("preset", 0))
        if self.preset not in {19, 21}:
            raise ValueError("vn30_tcbs preset must be 19 or 21")
        self.symbol = str(params.get("symbol", "41I1G8000")).upper()
        self.exchange = str(params.get("exchange", "tcbs")).lower()
        self.timeframe = str(params.get("timeframe", "5m"))
        if self.timeframe not in TIMEFRAME_MS:
            raise ValueError(f"unsupported vn30_tcbs timeframe: {self.timeframe}")
        self.qty = float(params.get("qty", 1.0))
        self.fee_pct = float(params.get("fee_pct", 0.0))
        self.leverage = int(params.get("leverage", 1))
        self.reverse = bool(params.get("reverse", False))
        self.timezone = ZoneInfo(
            str(params.get("session_timezone", "Asia/Ho_Chi_Minh"))
        )
        self.session_start = self._parse_clock(
            str(params.get("session_start", "08:45"))
        )
        self.force_flat_at = self._parse_clock(
            str(params.get("force_flat_at", "14:25"))
        )

        self.window = int(params.get("window", 60))
        self.window1 = int(params.get("window1", 10))
        self.window2 = int(params.get("window2", 40))
        self.threshold = float(params.get("threshold", 90.0))
        self.alma_period = int(params.get("period", 14))
        self.alma_sigma = float(params.get("sigma", 8.0))
        self.alma_threshold_bps = float(params.get("threshold_bps", 25.0))
        self.warmup_bars = int(
            params.get(
                "warmup_bars",
                max(self.window, self.window1, self.window2) + 2 * self.window + 2
                if self.preset == 19
                else self.alma_period,
            )
        )
        self.retain_bars = int(params.get("retain_bars", self.warmup_bars))
        self._pending_candle_open: int | None = None
        self._last_processed_candle = 0
        self._positions = self._load_positions()

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        return [f"kline:{params.get('timeframe', '5m')}"]

    def get_required_channels_instance(self) -> list[str]:
        return self.__class__.get_required_channels(self.params)

    def get_warmup_symbols(self) -> list[str]:
        return [self.symbol]

    def get_warmup_tfs(self) -> list[str]:
        return [self.timeframe]

    def get_warmup_bars(self, tf: str) -> int:
        return self.warmup_bars

    def get_retain_bars(self, tf: str) -> int:
        return max(self.warmup_bars, self.retain_bars)

    async def _shared_panel_bundle(self):
        return None

    def should_scan_after_event(
        self,
        kind: str,
        symbol: str | None = None,
        tf: str | None = None,
    ) -> bool:
        if kind != "kline" or symbol != self.symbol or tf != self.timeframe:
            return False
        latest = self.ctx.cache.get_latest_timestamp(self.symbol, self.timeframe)
        if latest is None or latest <= self._last_processed_candle:
            return False
        self._pending_candle_open = latest
        return True

    async def scan(self) -> None:
        if self._pending_candle_open is None:
            return
        candle_open = self._pending_candle_open
        self._pending_candle_open = None
        snapshot = self.ctx.cache.snapshot(
            self.symbol,
            self.timeframe,
            self.get_retain_bars(self.timeframe),
        )
        if not snapshot.times or snapshot.times[-1] != candle_open:
            return
        self._last_processed_candle = candle_open
        current_side = self._current_side()

        if self._must_force_flat(candle_open):
            await self._transition_to_side(
                0,
                float(snapshot.closes[-1]),
                candle_open,
                reason="SESSION_FLAT",
            )
            return
        if not self.ctx.state.ready or not self._inside_trade_session(candle_open):
            return

        # `current_side`/`desired_side` below are the strategy's actual traded
        # side. The decision engines (alpha19_decision / Alpha21AlmaCross) only
        # ever see and produce the "internal" side that the original, non-
        # reversed alpha would trade — cut_loss is self-referential state
        # scoped to that internal side, so we flip solely at this boundary and
        # leave the decision math untouched.
        decision_current_side = -current_side if self.reverse else current_side

        condition = 0
        diagnostics: dict[str, Any]
        if self.preset == 19:
            decision = alpha19_decision(
                snapshot.highs,
                snapshot.lows,
                snapshot.closes,
                current_side=decision_current_side,
                cut_loss=self._current_cut_loss(),
                window=self.window,
                window1=self.window1,
                window2=self.window2,
                threshold=self.threshold,
            )
            decision_side = decision.side
            condition = decision.condition
            desired_cut_loss = decision.cut_loss
            diagnostics = {
                "uo": decision.uo,
                "stochastic_rank": decision.stochastic_rank,
                "cut_loss": decision.cut_loss,
            }
        else:
            alma = Alpha21AlmaCross(
                period=self.alma_period,
                sigma=self.alma_sigma,
                threshold_bps=self.alma_threshold_bps,
            )
            alma.closes = [float(value) for value in snapshot.closes[:-1]]
            alma.side = decision_current_side
            decision_side = alma.on_bar(float(snapshot.closes[-1]))
            condition = alma.condition
            desired_cut_loss = None
            diagnostics = {
                "alma": alma.last_alma,
                "condition_semantics": "per_bar_trigger",
            }

        desired_side = -decision_side if self.reverse else decision_side

        logger.info(
            "[SIGNAL_AUDIT] %s",
            json.dumps(
                {
                    "alpha_id": self.alpha_id,
                    "preset": self.preset,
                    "reverse": self.reverse,
                    "symbol": self.symbol,
                    "timeframe": self.timeframe,
                    "signal_candle_open_ms": candle_open,
                    "current_side": current_side,
                    "condition": condition,
                    "desired_side": desired_side,
                    **diagnostics,
                },
                separators=(",", ":"),
                allow_nan=False,
            ),
            extra={"alpha_id": self.alpha_id},
        )
        await self._transition_to_side(
            desired_side,
            float(snapshot.closes[-1]),
            candle_open,
            reason="ALPHA_SIGNAL" if condition else "ALPHA_HOLD_OR_EXIT",
            cut_loss=desired_cut_loss,
        )

    async def manage_positions(self) -> None:
        return None

    def _load_positions(self) -> dict[str, dict[str, Any]]:
        source = self.ctx.load_authoritative_positions()
        if source is None:
            source = self.ctx.load_positions()
        if not isinstance(source, dict):
            return {}
        positions: dict[str, dict[str, Any]] = {}
        for key, value in source.items():
            if (
                not isinstance(value, dict)
                or str(value.get("symbol", "")).upper() != self.symbol
            ):
                continue
            position = dict(value)
            position_id = str(position.get("position_id") or key)
            position["position_id"] = position_id
            runtime = position.get("strategy_runtime")
            if not isinstance(runtime, dict):
                try:
                    metadata = json.loads(str(position.get("metadata", "{}")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                runtime = (
                    metadata.get("strategy_runtime")
                    if isinstance(metadata, dict)
                    else None
                )
            if isinstance(runtime, dict) and runtime.get("cut_loss") is not None:
                position["cut_loss"] = float(runtime["cut_loss"])
            positions[position_id] = position
        return positions

    def _persist_positions(self) -> None:
        if self._positions:
            self.ctx.save_positions(self._positions)
        else:
            self.ctx.clear_positions()

    def _current_position(self) -> dict[str, Any] | None:
        return next(iter(self._positions.values()), None)

    def _current_side(self) -> int:
        position = self._current_position()
        if position is None:
            return 0
        side = str(position.get("side", "")).upper()
        if side == "LONG":
            return 1
        if side == "SHORT":
            return -1
        return 0

    def _current_cut_loss(self) -> float | None:
        position = self._current_position()
        if position is None or position.get("cut_loss") is None:
            return None
        return float(position["cut_loss"])

    def _set_current_cut_loss(self, cut_loss: float | None) -> None:
        position = self._current_position()
        if position is not None:
            position["cut_loss"] = cut_loss

    async def _transition_to_side(
        self,
        desired_side: int,
        price: float,
        candle_open: int,
        *,
        reason: str,
        cut_loss: float | None = None,
    ) -> None:
        current_side = self._current_side()
        if desired_side == current_side:
            self._set_current_cut_loss(cut_loss)
            self._persist_positions()
            return

        for position_id in list(self._positions):
            await self.ctx.emit_signal(
                "CLOSE",
                position_id=position_id,
                symbol=self.symbol,
                reason=reason,
                exit_price=price,
                metadata=json.dumps(
                    {"ref_is_executable": False, "source": "vn30_tcbs_bar"},
                    sort_keys=True,
                ),
            )
            self._positions.pop(position_id, None)

        if desired_side == 0 or not self.ctx.can_open_trades():
            self._persist_positions()
            return

        side = "LONG" if desired_side > 0 else "SHORT"
        position_id = str(
            uuid.uuid5(
                POSITION_NAMESPACE,
                f"{self.alpha_id}|{self.version}|{candle_open}|{side}",
            )
        )
        runtime = {
            "preset": self.preset,
            "cut_loss": cut_loss,
            "condition_semantics": "per_bar_trigger" if self.preset == 21 else "direct",
        }
        metadata = {
            "strategy_runtime_version": 1,
            "strategy_runtime": runtime,
            "source_artifact": "alphavn30.html",
        }
        await self.ctx.emit_signal(
            "OPEN",
            position_id=position_id,
            symbol=self.symbol,
            side=side,
            entry=price,
            qty=self.qty,
            leverage=self.leverage,
            exchange=self.exchange,
            fee_pct=self.fee_pct,
            tf=self.timeframe,
            signal_candle_open_ms=candle_open,
            metadata=json.dumps(metadata, sort_keys=True),
        )
        self._positions[position_id] = {
            "position_id": position_id,
            "symbol": self.symbol,
            "side": side,
            "entry": price,
            "qty": self.qty,
            "exchange": self.exchange,
            "cut_loss": runtime["cut_loss"],
            "strategy_runtime": runtime,
        }
        self._persist_positions()

    def _inside_trade_session(self, candle_open: int) -> bool:
        close_clock = self._local_close_clock(candle_open)
        instant = datetime.fromtimestamp(
            (candle_open + TIMEFRAME_MS[self.timeframe]) / 1000.0,
            tz=timezone.utc,
        ).astimezone(self.timezone)
        return (
            instant.weekday() < 5
            and self.session_start <= close_clock < self.force_flat_at
        )

    def _must_force_flat(self, candle_open: int) -> bool:
        return self._local_close_clock(candle_open) >= self.force_flat_at

    def _local_close_clock(self, candle_open: int) -> wall_time:
        instant = datetime.fromtimestamp(
            (candle_open + TIMEFRAME_MS[self.timeframe]) / 1000.0,
            tz=timezone.utc,
        ).astimezone(self.timezone)
        return instant.time().replace(second=0, microsecond=0)

    @staticmethod
    def _parse_clock(value: str) -> wall_time:
        return datetime.strptime(value, "%H:%M").time()
