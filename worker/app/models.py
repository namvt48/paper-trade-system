from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SignalType(str, Enum):
    OPEN = "OPEN"
    MODIFY = "MODIFY"
    CLOSE = "CLOSE"
    REGISTER_COLUMNS = "REGISTER_COLUMNS"


@dataclass
class OpenSignal:
    type: SignalType
    alpha_id: str
    signal_id: str
    symbol: str
    side: str
    entry: float
    qty: float
    timestamp: str
    tp: Optional[float] = None
    sl: Optional[float] = None
    leverage: int = 1
    metadata: str = "{}"
    position_id: str = ""  # if provided by alpha, use it; else worker generates
    exchange: str = "binance"
    fee_pct: float = 0.0


@dataclass
class ModifySignal:
    type: SignalType
    alpha_id: str
    signal_id: str
    position_id: str
    timestamp: str
    tp: Optional[float] = None
    sl: Optional[float] = None
    metadata: str = "{}"


@dataclass
class CloseSignal:
    type: SignalType
    alpha_id: str
    signal_id: str
    position_id: str
    reason: str
    timestamp: str
    exit_price: Optional[float] = None
    qty: Optional[float] = None
    metadata: str = "{}"


@dataclass
class RegisterColumnsSignal:
    type: SignalType
    alpha_id: str
    signal_id: str
    columns: str


def _to_float(data: dict, key: str) -> Optional[float]:
    val = data.get(key)
    if val is None or val == "":
        return None
    return float(val)


def _to_int(data: dict, key: str, default: int = 1) -> int:
    val = data.get(key)
    if val is None or val == "":
        return default
    return int(val)


def parse_signal(data: dict):
    signal_type = data.get("type", "")
    try:
        st = SignalType(signal_type)
    except ValueError:
        raise ValueError(f"Unknown signal type: {signal_type}")

    if st == SignalType.OPEN:
        return OpenSignal(
            type=st,
            alpha_id=data["alpha_id"],
            signal_id=data["signal_id"],
            symbol=data["symbol"],
            side=data["side"],
            entry=_to_float(data, "entry") or 0.0,
            qty=_to_float(data, "qty") or 0.0,
            timestamp=data.get("timestamp", ""),
            tp=_to_float(data, "tp"),
            sl=_to_float(data, "sl"),
            leverage=_to_int(data, "leverage"),
            metadata=data.get("metadata", "{}"),
            position_id=data.get("position_id", ""),
            exchange=data.get("exchange", "binance"),
            fee_pct=float(data.get("fee_pct", 0.0) or 0.0),
        )
    elif st == SignalType.MODIFY:
        return ModifySignal(
            type=st,
            alpha_id=data["alpha_id"],
            signal_id=data["signal_id"],
            position_id=data.get("position_id", ""),
            timestamp=data.get("timestamp", ""),
            tp=_to_float(data, "tp"),
            sl=_to_float(data, "sl"),
            metadata=data.get("metadata", "{}"),
        )
    elif st == SignalType.CLOSE:
        return CloseSignal(
            type=st,
            alpha_id=data["alpha_id"],
            signal_id=data["signal_id"],
            position_id=data.get("position_id", ""),
            reason=data.get("reason", "SIGNAL"),
            timestamp=data.get("timestamp", ""),
            exit_price=_to_float(data, "exit_price"),
            qty=_to_float(data, "qty"),
            metadata=data.get("metadata", "{}"),
        )
    elif st == SignalType.REGISTER_COLUMNS:
        return RegisterColumnsSignal(
            type=st,
            alpha_id=data["alpha_id"],
            signal_id=data.get("signal_id", ""),
            columns=data.get("columns", "[]"),
        )
