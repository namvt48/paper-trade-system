import json
from unittest.mock import MagicMock

import pytest

from base.config import BaseConfig
from base.engine import BaseEngine
from base.models import SymbolData


class MockEngine(BaseEngine):
    def get_required_channels(self) -> list[str]:
        return ["kline:15m"]

    async def scan_loop(self) -> None:
        pass

    def _get_warmup_symbols(self) -> list[str]:
        return [s for s in ["BTCUSDT", "ETHUSDT"] if not self._is_blacklisted(s)]


@pytest.fixture
def engine():
    config = MagicMock(spec=BaseConfig)
    config.ALPHA_ID = "test-alpha"
    config.REDIS_URL = "redis://localhost:6379"
    config.REDIS_STREAM = "paper-signals"
    config.LOG_LEVEL = "INFO"
    config.LOG_DIR = "/tmp/test_logs"
    config.DATA_MAX_CANDLES = 1000
    config.WARMUP_BARS = 50
    config.SYMBOL_BLACKLIST = ""
    config.TF = "15m"
    return MockEngine(config)


@pytest.fixture
def engine_with_blacklist():
    config = MagicMock(spec=BaseConfig)
    config.ALPHA_ID = "test-alpha"
    config.REDIS_URL = "redis://localhost:6379"
    config.REDIS_STREAM = "paper-signals"
    config.LOG_LEVEL = "INFO"
    config.LOG_DIR = "/tmp/test_logs"
    config.DATA_MAX_CANDLES = 1000
    config.WARMUP_BARS = 50
    config.SYMBOL_BLACKLIST = "BTCUSDT,DOGEUSDT"
    config.TF = "15m"
    return MockEngine(config)


def test_engine_symbol_data_is_multi_tf(engine):
    assert isinstance(engine.symbol_data, dict)


def test_engine_on_kline_message_appends_to_tf(engine):
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67500.0,
            "low": 66800.0,
            "close": 67200.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": False,
        }
    )
    sd = engine.symbol_data["BTCUSDT"]["15m"]
    assert sd.price_list[-1] == 67200.0
    assert sd.high_list[-1] == 67500.0


def test_engine_on_kline_message_separates_tfs(engine):
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67500.0,
            "low": 66800.0,
            "close": 67200.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": False,
        }
    )
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "1h",
            "open": 67000.0,
            "high": 68000.0,
            "low": 66500.0,
            "close": 67500.0,
            "volume": 500.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": False,
        }
    )
    sd_15m = engine.symbol_data["BTCUSDT"]["15m"]
    sd_1h = engine.symbol_data["BTCUSDT"]["1h"]
    assert sd_15m.price_list[-1] == 67200.0
    assert sd_1h.price_list[-1] == 67500.0
    assert sd_1h.volume_list[-1] == 500.0


def test_engine_on_kline_message_correction_overwrites(engine):
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67500.0,
            "low": 66800.0,
            "close": 67200.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": False,
        }
    )
    engine.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67600.0,
            "low": 66800.0,
            "close": 67300.0,
            "volume": 110.0,
            "open_time": 1716768000000,
            "close_time": 1716771599999,
            "confirmed": True,
            "correction": True,
        }
    )
    sd = engine.symbol_data["BTCUSDT"]["15m"]
    assert len(sd.time_list) == 1
    assert sd.high_list[-1] == 67600.0
    assert sd.volume_list[-1] == 110.0


def test_engine_on_kline_multiple_candles(engine):
    base_ts = 1716768000000
    for i in range(3):
        engine.on_kline_message(
            {
                "symbol": "ETHUSDT",
                "tf": "15m",
                "open": 3000.0 + i,
                "high": 3050.0,
                "low": 2990.0,
                "close": 3020.0 + i,
                "volume": 100.0,
                "open_time": base_ts + i * 900000,
                "close_time": base_ts + i * 900000 + 899999,
                "confirmed": True,
                "correction": False,
            }
        )
    assert len(engine.symbol_data["ETHUSDT"]["15m"].time_list) == 3


def test_engine_on_kline_ignores_missing_symbol(engine):
    engine.on_kline_message(
        {"tf": "15m", "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "volume": 50.0, "open_time": 1716768000000}
    )
    assert len(engine.symbol_data) == 0


def test_engine_on_kline_ignores_missing_tf(engine):
    engine.on_kline_message(
        {"symbol": "BTCUSDT", "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "volume": 50.0, "open_time": 1716768000000}
    )
    assert len(engine.symbol_data) == 0


def test_engine_blacklist_skips_on_kline(engine_with_blacklist):
    engine_with_blacklist.on_kline_message(
        {
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67500.0,
            "low": 66800.0,
            "close": 67200.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "confirmed": True,
            "correction": False,
        }
    )
    assert "BTCUSDT" not in engine_with_blacklist.symbol_data


def test_engine_blacklist_allows_non_blacklisted(engine_with_blacklist):
    engine_with_blacklist.on_kline_message(
        {
            "symbol": "ETHUSDT",
            "tf": "15m",
            "open": 3000.0,
            "high": 3050.0,
            "low": 2990.0,
            "close": 3020.0,
            "volume": 100.0,
            "open_time": 1716768000000,
            "confirmed": True,
            "correction": False,
        }
    )
    assert "ETHUSDT" in engine_with_blacklist.symbol_data


def test_engine_is_blacklisted(engine):
    assert not engine._is_blacklisted("BTCUSDT")
    assert not engine._is_blacklisted("ETHUSDT")


def test_engine_is_blacklisted_with_blacklist(engine_with_blacklist):
    assert engine_with_blacklist._is_blacklisted("BTCUSDT")
    assert engine_with_blacklist._is_blacklisted("DOGEUSDT")
    assert not engine_with_blacklist._is_blacklisted("ETHUSDT")


def test_engine_load_warmup_candles(engine):
    data = {
        "symbol": "BTCUSDT",
        "tf": "15m",
        "candles": json.dumps([
            {"open_time": 1716768000000, "open": 67000.0, "high": 67500.0, "low": 66800.0, "close": 67200.0, "volume": 100.0},
            {"open_time": 1716768900000, "open": 67200.0, "high": 67800.0, "low": 67100.0, "close": 67500.0, "volume": 120.0},
        ]),
    }
    engine._load_warmup_candles(data)
    sd = engine.symbol_data["BTCUSDT"]["15m"]
    assert len(sd.time_list) == 2
    assert sd.price_list[0] == 67200.0
    assert sd.price_list[1] == 67500.0


def test_engine_load_warmup_candles_skips_blacklisted(engine_with_blacklist):
    data = {
        "symbol": "BTCUSDT",
        "tf": "15m",
        "candles": json.dumps([
            {"open_time": 1716768000000, "open": 67000.0, "high": 67500.0, "low": 66800.0, "close": 67200.0, "volume": 100.0},
        ]),
    }
    engine_with_blacklist._load_warmup_candles(data)
    assert "BTCUSDT" not in engine_with_blacklist.symbol_data


def test_engine_get_warmup_symbols_subtracts_blacklist(engine_with_blacklist):
    symbols = engine_with_blacklist._get_warmup_symbols()
    assert "BTCUSDT" not in symbols
    assert "ETHUSDT" in symbols
