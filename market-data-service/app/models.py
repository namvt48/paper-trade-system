from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KlineCandle:
    symbol: str
    tf: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_time: int
    close_time: int
    confirmed: bool = True
    correction: bool = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "tf": self.tf,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "confirmed": self.confirmed,
            "correction": self.correction,
        }

    @classmethod
    def from_ws_1m(cls, payload: dict) -> "KlineCandle | None":
        payload = payload.get("data", payload)
        if payload.get("e") != "kline":
            return None

        kline = payload.get("k", {})
        if not kline.get("x", False):
            return None

        return cls(
            symbol=payload.get("s", ""),
            tf="1m",
            open=float(kline["o"]),
            high=float(kline["h"]),
            low=float(kline["l"]),
            close=float(kline["c"]),
            volume=float(kline["v"]),
            open_time=int(kline["t"]),
            close_time=int(kline["T"]),
            confirmed=True,
            correction=False,
        )


@dataclass
class TickerUpdate:
    symbol: str
    price: float
    timestamp: int
    exchange: str = "binance"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "timestamp": self.timestamp,
            "exchange": self.exchange,
        }

    @classmethod
    def from_binance_ws(cls, msg: dict) -> "TickerUpdate":
        msg = msg.get("data", msg)
        return cls(
            symbol=msg.get("s", ""),
            price=float(msg.get("c", 0)),
            timestamp=int(msg.get("E", 0)),
            exchange="binance",
        )
