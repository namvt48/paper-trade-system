from __future__ import annotations

import json

from runner.checkpoint_virtual_ledger import _latest_prices
from runner.virtual_trade_ledger import VirtualTradeLedgerPublisher


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands: list[tuple[str, object]] = []

    def xadd(self, stream: str, fields: dict[str, str]):
        self.commands.append(("xadd", (stream, fields)))
        return self

    def set(self, key: str, value: str):
        self.commands.append(("set", (key, value)))
        return self

    def execute(self):
        for command, args in self.commands:
            if command == "xadd":
                stream, fields = args
                self.redis.streams.setdefault(stream, []).append(fields)
            elif command == "set":
                key, value = args
                self.redis.values[key] = value
        return [True] * len(self.commands)


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.streams: dict[str, list[dict[str, str]]] = {}

    def get(self, key: str):
        return self.values.get(key)

    def pipeline(self, transaction: bool = True):
        assert transaction is True
        return _Pipeline(self)


class _MdsRedis:
    def __init__(self, rows: dict[str, dict]):
        self.rows = rows

    def lindex(self, key: str, index: int):
        assert index == 0
        row = self.rows.get(key)
        return json.dumps(row) if row else None


def _events(redis: _Redis) -> list[dict]:
    return [
        json.loads(row["payload"])
        for row in redis.streams.get("paper-shadow-trades", [])
    ]


def test_rebalance_persists_virtual_positions_without_real_signal_stream():
    redis = _Redis()
    ledger = VirtualTradeLedgerPublisher(redis, "sleeve-a", 10_000, "binance")

    ledger.rebalance(
        weights={"BTCUSDT": 0.5, "ETHUSDT": -0.5},
        prices={"BTCUSDT": 100.0, "ETHUSDT": 200.0},
        candle_open_ms=1_000,
        timeframe="1h",
        metadata_by_symbol={},
    )

    events = _events(redis)
    assert {event["type"] for event in events} == {"VIRTUAL_OPEN"}
    assert {event["symbol"] for event in events} == {"BTCUSDT", "ETHUSDT"}
    assert {event["qty"] for event in events} == {50.0, 25.0}
    assert "paper-signals" not in redis.streams
    assert json.loads(redis.values["shadow:ledger:positions:sleeve-a"])


def test_rebalance_closes_prior_virtual_positions_and_is_deterministic():
    redis = _Redis()
    first = VirtualTradeLedgerPublisher(redis, "sleeve-a", 10_000, "binance")
    first.rebalance(
        weights={"BTCUSDT": 1.0},
        prices={"BTCUSDT": 100.0},
        candle_open_ms=1_000,
        timeframe="1h",
        metadata_by_symbol={},
    )

    restarted = VirtualTradeLedgerPublisher(redis, "sleeve-a", 10_000, "binance")
    restarted.rebalance(
        weights={},
        prices={"BTCUSDT": 110.0},
        candle_open_ms=2_000,
        timeframe="1h",
        metadata_by_symbol={},
    )

    events = _events(redis)
    close = next(event for event in events if event["type"] == "VIRTUAL_CLOSE")
    opened = next(event for event in events if event["type"] == "VIRTUAL_OPEN")
    assert close["position_id"] == opened["position_id"]
    assert close["price"] == 110.0
    assert json.loads(redis.values["shadow:ledger:positions:sleeve-a"]) == {}


def test_missing_state_bootstraps_current_target_book():
    redis = _Redis()
    redis.values["book:target:sleeve-a"] = json.dumps(
        {
            "sleeve_id": "sleeve-a",
            "generated_at": "2026-07-25T00:00:00+00:00",
            "as_of_candle_ms": 1_000,
            "timeframe": "1h",
            "weights": {"BTCUSDT": 1.0},
            "meta": {"prices": {"BTCUSDT": 100.0}},
        }
    )

    VirtualTradeLedgerPublisher(redis, "sleeve-a", 10_000, "binance")

    event = _events(redis)[0]
    assert event["type"] == "VIRTUAL_OPEN"
    assert event["timestamp"] == "2026-07-25T00:00:00+00:00"
    assert event["metadata"]["bootstrap_source"] == "target_book"


def test_checkpoint_prices_use_latest_confirmed_native_candle():
    mds = _MdsRedis(
        {
            "kline_snapshot_v2:binance:1h:BTCUSDT": {
                "open_time": 2_000,
                "close": 110.0,
            },
            "kline_snapshot_v2:binance:1h:ETHUSDT": {
                "open_time": 2_000,
                "close": 210.0,
            },
        }
    )

    prices, latest = _latest_prices(
        mds, "binance", "1h", ["BTCUSDT", "ETHUSDT"]
    )

    assert prices == {"BTCUSDT": 110.0, "ETHUSDT": 210.0}
    assert latest == 2_000
