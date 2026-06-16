import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import redis as redis_lib
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

    async def _manage_positions(self) -> None:
        pass

    def _has_open_positions(self) -> bool:
        return False


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
    config.WARMUP_MIN_SYMBOL_COVERAGE = 0.90
    config.SYMBOL_BLACKLIST = ""
    config.TF = "15m"
    config.MANAGE_INTERVAL_SEC = 60.0
    config.MDS_REDIS_URL = ""
    config.MDS_EXCHANGE = ""
    config.PRICE_ALERT_SYNC_INTERVAL_SEC = 5.0
    config.PRICE_ALERT_STALE_SEC = 15.0
    config.POSITION_RECONCILE_INTERVAL_SEC = 5.0
    config.POSITION_SNAPSHOT_MAX_AGE_SEC = 15.0
    config.ALPHA_RUNTIME_HEARTBEAT_TTL_SEC = 20
    config.POSITION_RECONCILE_STARTUP_TIMEOUT_SEC = 30.0
    config.RECONNECT_WARMUP_SKIP_IF_FRESH_SEC = 300.0
    config.RECONCILE_NO_POSITION_IS_OK = True
    config.RECONCILE_STALE_SUSPEND_NEW_ENTRIES = True
    config.DATA_STALE_SUSPEND_NEW_ENTRIES = True
    config.PRICE_ALERT_SYNC_SUSPEND_NEW_ENTRIES = True
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
    config.WARMUP_MIN_SYMBOL_COVERAGE = 0.90
    config.SYMBOL_BLACKLIST = "BTCUSDT,DOGEUSDT"
    config.TF = "15m"
    config.MANAGE_INTERVAL_SEC = 60.0
    config.MDS_REDIS_URL = ""
    config.MDS_EXCHANGE = ""
    config.PRICE_ALERT_SYNC_INTERVAL_SEC = 5.0
    config.PRICE_ALERT_STALE_SEC = 15.0
    config.POSITION_RECONCILE_INTERVAL_SEC = 5.0
    config.POSITION_SNAPSHOT_MAX_AGE_SEC = 15.0
    config.ALPHA_RUNTIME_HEARTBEAT_TTL_SEC = 20
    config.POSITION_RECONCILE_STARTUP_TIMEOUT_SEC = 30.0
    config.RECONNECT_WARMUP_SKIP_IF_FRESH_SEC = 300.0
    config.RECONCILE_NO_POSITION_IS_OK = True
    config.RECONCILE_STALE_SUSPEND_NEW_ENTRIES = True
    config.DATA_STALE_SUSPEND_NEW_ENTRIES = True
    config.PRICE_ALERT_SYNC_SUSPEND_NEW_ENTRIES = True
    return MockEngine(config)


@pytest.fixture
def mds_engine(engine):
    engine.config.MDS_REDIS_URL = "redis://localhost:6381"
    engine.config.MDS_EXCHANGE = "binance"
    return engine


def test_engine_symbol_data_is_multi_tf(engine):
    assert isinstance(engine.symbol_data, dict)


def test_engine_config_has_runtime_safety_fields():
    assert "RECONNECT_WARMUP_SKIP_IF_FRESH_SEC" in BaseConfig.model_fields
    assert "RECONCILE_NO_POSITION_IS_OK" in BaseConfig.model_fields
    assert "RECONCILE_STALE_SUSPEND_NEW_ENTRIES" in BaseConfig.model_fields
    assert "DATA_STALE_SUSPEND_NEW_ENTRIES" in BaseConfig.model_fields
    assert "PRICE_ALERT_SYNC_SUSPEND_NEW_ENTRIES" in BaseConfig.model_fields


def test_claim_position_candle_rejects_entry_candle_and_duplicates(engine):
    entry_open_ms = 1_000_000
    candle_ms = 900_000
    position = {
        "entry_candle_open_ms": entry_open_ms,
        "signal_candle_close_ms": entry_open_ms + candle_ms,
    }

    assert engine._claim_position_candle(position, entry_open_ms) is False
    assert "last_strategy_candle_ms" not in position

    next_candle_ms = entry_open_ms + candle_ms
    assert engine._claim_position_candle(position, next_candle_ms) is True
    assert position["last_strategy_candle_ms"] == next_candle_ms
    assert engine._claim_position_candle(position, next_candle_ms) is False
    assert engine._claim_position_candle(position, next_candle_ms + candle_ms) is True


def test_claim_position_candle_supports_legacy_position_without_timing(engine):
    position = {}

    assert engine._claim_position_candle(position, 1_000_000) is True
    assert engine._claim_position_candle(position, 1_000_000) is False


def test_engine_stale_threshold_respects_kline_timeframe(mds_engine):
    assert mds_engine._stale_threshold_seconds() == pytest.approx(2250.0)


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


def test_engine_on_kline_out_of_order_inserts_sorted(engine):
    times = [1716769800000, 1716768000000, 1716768900000]
    for open_time in times:
        engine.on_kline_message(
            {
                "symbol": "ETHUSDT",
                "tf": "15m",
                "open": 3000.0,
                "high": 3050.0,
                "low": 2990.0,
                "close": float(open_time),
                "volume": 100.0,
                "open_time": open_time,
                "confirmed": True,
                "correction": False,
            }
        )
    assert engine.symbol_data["ETHUSDT"]["15m"].time_list == sorted(times)


def test_engine_on_kline_trims_to_data_max(engine):
    engine.config.DATA_MAX_CANDLES = 2
    base_ts = 1716768000000
    for i in range(3):
        engine.on_kline_message(
            {
                "symbol": "ETHUSDT",
                "tf": "15m",
                "open": 3000.0,
                "high": 3050.0,
                "low": 2990.0,
                "close": 3020.0 + i,
                "volume": 100.0,
                "open_time": base_ts + i * 900000,
                "confirmed": True,
                "correction": False,
            }
        )
    sd = engine.symbol_data["ETHUSDT"]["15m"]
    assert len(sd.time_list) == 2
    assert sd.time_list == [base_ts + 900000, base_ts + 2 * 900000]


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


def test_engine_auto_appends_kline_1m(engine):
    channels = engine._build_channels()
    assert "kline:1m" in channels


def test_engine_no_duplicate_kline_1m(engine):
    channels = engine._build_channels()
    assert channels.count("kline:1m") <= 1


def test_engine_no_append_kline_1m_when_disabled(engine):
    engine.config.MANAGE_INTERVAL_SEC = 0
    channels = engine._build_channels()
    assert "kline:1m" not in channels


def test_engine_mds_kline_channel_is_exchange_prefixed(mds_engine):
    channels = mds_engine._build_channels()
    assert "kline:binance:15m" in channels
    assert "kline:15m" not in channels


def test_engine_mds_does_not_subscribe_full_ticker(mds_engine):
    channels = mds_engine._build_channels()
    assert "ticker:binance" not in channels


def test_engine_price_alert_channel_helpers(mds_engine):
    assert mds_engine._price_alert_subscribe_channel() == "price_alert:subscribe:binance"
    assert mds_engine._price_alert_channel("BTCUSDT") == "price_alert:binance:BTCUSDT"


def test_engine_publish_price_alert_sync_payload(mds_engine):
    redis_client = MagicMock()
    mds_engine._publish_price_alert_sync(redis_client, {"ETHUSDT", "BTCUSDT"})
    redis_client.publish.assert_called_once()
    channel, payload_raw = redis_client.publish.call_args.args
    assert channel == "price_alert:subscribe:binance"
    payload = json.loads(payload_raw)
    assert payload == {
        "consumer_id": "test-alpha",
        "action": "sync",
        "symbols": ["BTCUSDT", "ETHUSDT"],
    }


def test_engine_active_position_symbols(mds_engine):
    mds_engine._open_positions = {"BTCUSDT": {"side": "LONG"}}
    assert mds_engine._get_active_position_symbols() == {"BTCUSDT"}


def test_engine_trigger_price_long_is_side_aware(engine):
    assert engine._trigger_price("LONG", {"bid": 10, "last": 11, "price": 12}) == 10
    assert engine._trigger_price("LONG", {"bid": None, "last": 11, "price": 12}) == 11
    assert engine._trigger_price("LONG", {"bid": 0, "last": None, "price": 12}) == 12


def test_engine_trigger_price_short_is_side_aware(engine):
    assert engine._trigger_price("SHORT", {"ask": 13, "last": 11, "price": 12}) == 13
    assert engine._trigger_price("SHORT", {"ask": None, "last": 11, "price": 12}) == 11
    assert engine._trigger_price("SHORT", {"ask": 0, "last": None, "price": 12}) == 12


def test_engine_can_open_new_trades_only_when_live(engine):
    engine.runtime_state = "STALE"
    assert engine.can_open_new_trades() is False
    engine.runtime_state = "LIVE"
    assert engine.can_open_new_trades() is True


def test_engine_can_open_new_trades_requires_all_safety_flags_clear(mds_engine):
    mds_engine.runtime_state = "LIVE"
    mds_engine._data_stale = False
    mds_engine._position_reconcile_stale = False
    mds_engine._price_alert_sync_stale = False
    assert mds_engine.can_open_new_trades() is True

    mds_engine._data_stale = True
    assert mds_engine.can_open_new_trades() is False
    mds_engine._data_stale = False

    mds_engine._position_reconcile_stale = True
    assert mds_engine.can_open_new_trades() is False
    mds_engine._position_reconcile_stale = False

    mds_engine._price_alert_sync_stale = True
    assert mds_engine.can_open_new_trades() is False


def test_engine_reconnect_skip_uses_tf_aware_freshness(mds_engine):
    mds_engine.config.TF = "15m"
    mds_engine.config.RECONNECT_WARMUP_SKIP_IF_FRESH_SEC = 300.0
    mds_engine.config.WARMUP_MIN_SYMBOL_COVERAGE = 0.90
    now_ms = int(time.time() * 1000)
    ten_minutes_ago_ms = now_ms - 600_000
    mds_engine.symbols = ["BTCUSDT"]
    mds_engine.symbol_data["BTCUSDT"] = {"15m": SymbolData(
        price_list=[1.0], volume_list=[1.0], high_list=[1.0],
        low_list=[1.0], open_list=[1.0], time_list=[ten_minutes_ago_ms],
    )}
    mds_engine.last_redis_message_at = time.time()
    assert mds_engine._should_skip_reconnect_warmup("15m") is True


def test_engine_reconnect_does_not_skip_when_coverage_below_required(mds_engine):
    mds_engine.config.TF = "15m"
    mds_engine.config.WARMUP_MIN_SYMBOL_COVERAGE = 0.90
    mds_engine.symbols = ["BTCUSDT", "ETHUSDT"]
    now_ms = int(time.time() * 1000)
    mds_engine.symbol_data["BTCUSDT"] = {"15m": SymbolData(
        price_list=[1.0], volume_list=[1.0], high_list=[1.0],
        low_list=[1.0], open_list=[1.0], time_list=[now_ms],
    )}
    mds_engine.last_redis_message_at = time.time()
    assert mds_engine._should_skip_reconnect_warmup("15m") is False


def test_engine_data_stale_suspends_entries_but_does_not_break_listener(mds_engine):
    mds_engine.runtime_state = "LIVE"
    mds_engine._data_stale = True
    assert mds_engine.can_open_new_trades() is False
    assert mds_engine._stale_should_break_listener() is False


def test_engine_transport_failure_can_break_listener(mds_engine):
    mds_engine._transport_reconnect_requested = True
    assert mds_engine._stale_should_break_listener() is True


def test_price_alert_sync_failure_only_sets_price_alert_flag(mds_engine):
    mds_engine.runtime_state = "LIVE"
    mds_engine._mark_price_alert_sync_failed(redis_lib.RedisError("boom"))
    assert mds_engine._price_alert_sync_stale is True
    assert mds_engine._data_stale is False
    assert mds_engine._position_reconcile_stale is False
    assert mds_engine.runtime_state == "LIVE"
    assert mds_engine.can_open_new_trades() is False


def test_price_alert_sync_recovery_only_clears_price_alert_flag(mds_engine):
    mds_engine.runtime_state = "STALE"
    mds_engine._data_stale = True
    mds_engine._price_alert_sync_stale = True
    mds_engine._mark_price_alert_sync_recovered()
    assert mds_engine._price_alert_sync_stale is False
    assert mds_engine._data_stale is True
    assert mds_engine.runtime_state == "STALE"


def test_engine_mds_ignores_wrong_exchange_kline(mds_engine):
    mds_engine.on_kline_message(
        {
            "exchange": "okx",
            "symbol": "BTCUSDT",
            "tf": "15m",
            "open": 67000.0,
            "high": 67500.0,
            "low": 66800.0,
            "close": 67200.0,
            "volume": 100.0,
            "open_time": 1716768000000,
        }
    )
    assert "BTCUSDT" not in mds_engine.symbol_data


def test_manage_loop_skips_when_no_open_positions(engine):
    assert engine._has_open_positions() is False


def test_engine_empty_sync_sends_empty_symbols(mds_engine):
    redis_client = MagicMock()
    mds_engine._publish_price_alert_sync(redis_client, set())
    redis_client.publish.assert_called_once()
    channel, payload_raw = redis_client.publish.call_args.args
    assert channel == "price_alert:subscribe:binance"
    payload = json.loads(payload_raw)
    assert payload["symbols"] == []
    assert payload["action"] == "sync"


def test_engine_price_alert_channel_for_active_position(mds_engine):
    mds_engine._open_positions = {"BTCUSDT": {"side": "LONG"}}
    symbols = mds_engine._get_active_position_symbols()
    assert symbols == {"BTCUSDT"}
    ch = mds_engine._price_alert_channel("BTCUSDT")
    assert ch == "price_alert:binance:BTCUSDT"


def test_engine_warmup_request_id_is_unique(mds_engine):
    id1 = mds_engine._make_warmup_request_id("15m")
    id2 = mds_engine._make_warmup_request_id("15m")
    assert id1 != id2
    assert id1.startswith("test-alpha:warmup:binance:15m:")
    assert id2.startswith("test-alpha:warmup:binance:15m:")


def test_engine_tf_to_ms():
    assert BaseEngine._tf_to_ms("1m") == 60_000
    assert BaseEngine._tf_to_ms("15m") == 900_000
    assert BaseEngine._tf_to_ms("1h") == 3_600_000
    assert BaseEngine._tf_to_ms("4h") == 14_400_000
    assert BaseEngine._tf_to_ms("1d") == 86_400_000


def test_engine_mds_subscribes_symbol_universe_channel(mds_engine):
    channels = mds_engine._build_channels()
    assert "symbols:binance" in channels


def test_engine_legacy_mode_does_not_subscribe_symbol_universe_channel(engine):
    channels = engine._build_channels()
    assert not any(ch.startswith("symbols:") for ch in channels)


@pytest.mark.asyncio
async def test_engine_snapshot_loads_fresh_candles(mds_engine):
    tf = "15m"
    candle_ms = 900_000
    now_ms = int(time.time() * 1000)
    candles = []
    for i in range(50):
        open_time = now_ms - (50 - i) * candle_ms
        candles.append({
            "symbol": "BTCUSDT", "tf": tf, "open_time": open_time,
            "open": 60000.0, "high": 60100.0, "low": 59900.0, "close": 60050.0, "volume": 100.0,
        })

    redis_mock = MagicMock()
    redis_mock.lrange.return_value = []
    redis_mock.hgetall.return_value = {str(c["open_time"]): json.dumps(c) for c in candles}

    loaded = await mds_engine._try_snapshot_warmup(redis_mock, ["BTCUSDT"], tf, 50)

    assert "BTCUSDT" in loaded
    assert "BTCUSDT" in mds_engine.symbol_data
    assert len(mds_engine.symbol_data["BTCUSDT"][tf].time_list) == 50


@pytest.mark.asyncio
async def test_engine_snapshot_prefers_v2_list(mds_engine):
    tf = "15m"
    candle_ms = 900_000
    now_ms = int(time.time() * 1000)
    v2_candles = []
    legacy_candles = []
    for i in range(50):
        open_time = now_ms - (50 - i) * candle_ms
        v2_candles.append({
            "symbol": "BTCUSDT", "tf": tf, "open_time": open_time,
            "open": 60000.0, "high": 60100.0, "low": 59900.0, "close": 60050.0, "volume": 100.0,
        })
        legacy_candles.append({
            "symbol": "BTCUSDT", "tf": tf, "open_time": open_time,
            "open": 61000.0, "high": 61100.0, "low": 60900.0, "close": 61050.0, "volume": 100.0,
        })

    redis_mock = MagicMock()
    redis_mock.lrange.return_value = [json.dumps(c) for c in reversed(v2_candles)]
    redis_mock.hgetall.return_value = {str(c["open_time"]): json.dumps(c) for c in legacy_candles}

    loaded = await mds_engine._try_snapshot_warmup(redis_mock, ["BTCUSDT"], tf, 50)

    assert loaded == {"BTCUSDT"}
    assert mds_engine.symbol_data["BTCUSDT"][tf].price_list[-1] == pytest.approx(60050.0)
    redis_mock.hgetall.assert_not_called()


@pytest.mark.asyncio
async def test_engine_snapshot_falls_back_to_legacy_hash(mds_engine):
    tf = "15m"
    candle_ms = 900_000
    now_ms = int(time.time() * 1000)
    candles = []
    for i in range(50):
        open_time = now_ms - (50 - i) * candle_ms
        candles.append({
            "symbol": "BTCUSDT", "tf": tf, "open_time": open_time,
            "open": 61000.0, "high": 61100.0, "low": 60900.0, "close": 61050.0, "volume": 100.0,
        })

    redis_mock = MagicMock()
    redis_mock.lrange.return_value = []
    redis_mock.hgetall.return_value = {str(c["open_time"]): json.dumps(c) for c in candles}

    loaded = await mds_engine._try_snapshot_warmup(redis_mock, ["BTCUSDT"], tf, 50)

    assert loaded == {"BTCUSDT"}
    assert len(mds_engine.symbol_data["BTCUSDT"][tf].time_list) == 50
    assert mds_engine.symbol_data["BTCUSDT"][tf].price_list[-1] == pytest.approx(61050.0)


@pytest.mark.asyncio
async def test_request_warmup_uses_snapshot_fast_path_for_large_bars(mds_engine, monkeypatch):
    mds_engine.config.WARMUP_BARS = 8641
    mds_engine.config.WARMUP_TIMEOUT_SEC = 1
    mds_engine.config.WARMUP_MIN_SYMBOL_COVERAGE = 0.90

    redis_mock = MagicMock()
    redis_mock.ping.return_value = True

    async def fake_connect(url):
        return redis_mock

    async def fake_snapshot(redis_client, symbols, tf, bars):
        assert symbols == ["BTCUSDT"]
        assert bars == 8641
        now_ms = int(time.time() * 1000)
        mds_engine.symbol_data["BTCUSDT"] = {"15m": SymbolData(
            price_list=[1.0], volume_list=[1.0], high_list=[1.0],
            low_list=[1.0], open_list=[1.0], time_list=[now_ms],
        )}
        return {"BTCUSDT"}

    monkeypatch.setattr(mds_engine, "_get_warmup_symbols", lambda: ["BTCUSDT"])
    monkeypatch.setattr(mds_engine, "_connect_redis", fake_connect)
    monkeypatch.setattr(mds_engine, "_try_snapshot_warmup", fake_snapshot)
    result = await mds_engine._request_warmup()
    assert result is True


@pytest.mark.asyncio
async def test_engine_snapshot_ignores_stale(mds_engine):
    tf = "15m"
    candle_ms = 900_000
    now_ms = int(time.time() * 1000)
    # Latest candle is 3 candles ago (stale threshold is 2x candle_ms)
    stale_base = now_ms - 3 * candle_ms
    candles = []
    for i in range(50):
        open_time = stale_base - (50 - i) * candle_ms
        candles.append({
            "symbol": "BTCUSDT", "tf": tf, "open_time": open_time,
            "open": 60000.0, "high": 60100.0, "low": 59900.0, "close": 60050.0, "volume": 100.0,
        })

    redis_mock = MagicMock()
    redis_mock.lrange.return_value = []
    redis_mock.hgetall.return_value = {str(c["open_time"]): json.dumps(c) for c in candles}

    loaded = await mds_engine._try_snapshot_warmup(redis_mock, ["BTCUSDT"], tf, 50)

    assert "BTCUSDT" not in loaded
    assert "BTCUSDT" not in mds_engine.symbol_data


def test_engine_build_candle_close_metadata_long_sl(engine):
    meta_str = engine._build_candle_close_metadata(
        reason="SL",
        stop_price=0.0615,
        trigger_price=0.0589,
        fill_price=0.0589,
        candle_high=0.0620,
        candle_low=0.0589,
        tf="1m",
    )
    meta = json.loads(meta_str)
    assert meta["close_model"] == "candle_fallback_conservative"
    assert meta["reason"] == "SL"
    assert meta["stop_price"] == pytest.approx(0.0615)
    assert meta["trigger_price"] == pytest.approx(0.0589)
    assert meta["raw_fill_price"] == pytest.approx(0.0589)
    assert meta["candle_high"] == pytest.approx(0.0620)
    assert meta["candle_low"] == pytest.approx(0.0589)
    assert meta["tf"] == "1m"
    assert meta["source"] == "kline"


def test_engine_build_candle_close_metadata_short_sl(engine):
    meta = json.loads(engine._build_candle_close_metadata(
        reason="SL",
        stop_price=0.065,
        trigger_price=0.0661,
        fill_price=0.0661,
        candle_high=0.0661,
        candle_low=0.0640,
    ))
    assert meta["close_model"] == "candle_fallback_conservative"
    assert meta["trigger_price"] == pytest.approx(0.0661)
    assert meta["raw_fill_price"] == pytest.approx(0.0661)
    assert meta["tf"] == "1m"


def test_engine_build_candle_close_metadata_long_tp(engine):
    meta = json.loads(engine._build_candle_close_metadata(
        reason="TP",
        stop_price=0.070,
        trigger_price=0.0710,
        fill_price=0.070,
        candle_high=0.0710,
        candle_low=0.0680,
    ))
    assert meta["reason"] == "TP"
    assert meta["trigger_price"] == pytest.approx(0.0710)
    assert meta["raw_fill_price"] == pytest.approx(0.070)


def test_engine_candle_sl_fill_is_not_stop_level(engine):
    """LONG SL via candle: fill must be candle low, not stop level."""
    sl = 0.0615
    candle_low = 0.0589
    # candle_low < sl — fill at candle_low (worse), not at sl
    meta = json.loads(engine._build_candle_close_metadata(
        reason="SL",
        stop_price=sl,
        trigger_price=candle_low,
        fill_price=candle_low,
        candle_high=0.0620,
        candle_low=candle_low,
    ))
    assert meta["raw_fill_price"] == pytest.approx(candle_low)
    assert meta["raw_fill_price"] != pytest.approx(sl)


def test_engine_build_close_metadata_price_alert(engine):
    tick = {"bid": 0.058865, "ask": 0.058875, "last": None, "price": 0.05887, "timestamp": 1780290087000, "source": "bookTicker"}
    meta_str = engine._build_close_metadata(
        reason="SL",
        stop_price=0.061485511,
        trigger_price=0.058865,
        tick=tick,
    )
    import json
    meta = json.loads(meta_str)
    assert meta["close_model"] == "price_alert_side_aware"
    assert meta["reason"] == "SL"
    assert meta["stop_price"] == pytest.approx(0.061485511)
    assert meta["trigger_price"] == pytest.approx(0.058865)
    assert meta["raw_fill_price"] == pytest.approx(0.058865)
    assert meta["bid"] == pytest.approx(0.058865)
    assert meta["ask"] == pytest.approx(0.058875)
    assert meta["tick_timestamp"] == 1780290087000
    assert meta["source"] == "bookTicker"
    assert meta["ref_is_executable"] is True


def test_engine_build_close_metadata_custom_model(engine):
    import json
    meta = json.loads(engine._build_close_metadata(
        reason="TP",
        stop_price=0.065,
        trigger_price=0.065,
        tick={"bid": 0.065, "ask": 0.0651},
        close_model="candle_fallback_conservative",
    ))
    assert meta["close_model"] == "candle_fallback_conservative"
    assert meta["ref_is_executable"] is False


def test_engine_long_sl_trigger_uses_bid(engine):
    """LONG SL hit: trigger_price should use bid, not sl level."""
    tick = {"bid": 0.058865, "ask": 0.058875, "price": 0.05887}
    trigger = engine._trigger_price("LONG", tick)
    assert trigger == pytest.approx(0.058865)
    # Confirm bid < hypothetical sl would produce exit at bid, not sl
    sl = 0.061485511
    assert trigger < sl  # trigger caused SL, fill is at trigger (bid)


def test_engine_short_sl_trigger_uses_ask(engine):
    """SHORT SL hit: trigger_price should use ask."""
    tick = {"bid": 0.068, "ask": 0.0681, "price": 0.0680}
    trigger = engine._trigger_price("SHORT", tick)
    assert trigger == pytest.approx(0.0681)


def test_engine_trigger_price_returns_none_when_no_valid_price(engine):
    assert engine._trigger_price("LONG", {"bid": None, "last": None, "price": None}) is None
    assert engine._trigger_price("LONG", {"bid": 0, "last": 0, "price": 0}) is None
    assert engine._trigger_price("SHORT", {}) is None


@pytest.mark.asyncio
async def test_engine_snapshot_skips_if_insufficient_bars(mds_engine):
    tf = "15m"
    candle_ms = 900_000
    now_ms = int(time.time() * 1000)
    # Only 10 candles in snapshot, but 50 required
    candles = [{"symbol": "BTCUSDT", "tf": tf, "open_time": now_ms - i * candle_ms,
                "open": 60000.0, "high": 60100.0, "low": 59900.0, "close": 60050.0, "volume": 100.0}
               for i in range(10)]

    redis_mock = MagicMock()
    redis_mock.lrange.return_value = []
    redis_mock.hgetall.return_value = {str(c["open_time"]): json.dumps(c) for c in candles}

    loaded = await mds_engine._try_snapshot_warmup(redis_mock, ["BTCUSDT"], tf, 50)

    assert "BTCUSDT" not in loaded


@pytest.mark.asyncio
async def test_engine_warmup_timeout_returns_false(mds_engine):
    """When the warmup stream returns no data (timeout), _request_warmup returns False.
    This means the engine stays STALE and cannot open new trades.
    """
    mds_engine.config.INITIAL_DATA_TIMEOUT_SEC = 0.1
    mds_engine.config.WARMUP_BARS = 501  # above snapshot threshold to force stream path

    class RedisNoWarmupResponse:
        def ping(self):
            return True

        def xadd(self, *args, **kwargs):
            return "1-0"

        def xread(self, *args, **kwargs):
            return []

        def delete(self, *args, **kwargs):
            return 1

        def close(self):
            pass

    redis_mock = RedisNoWarmupResponse()

    with (
        patch.object(mds_engine, "_try_snapshot_warmup", new=AsyncMock(return_value=set())),
        patch.object(mds_engine, "_connect_redis", new=AsyncMock(return_value=redis_mock)),
    ):
        warmup_ok = await mds_engine._request_warmup()

    assert warmup_ok is False
    mds_engine.runtime_state = "LIVE" if warmup_ok else "STALE"
    assert mds_engine.runtime_state == "STALE"
    assert not mds_engine.can_open_new_trades()


def test_engine_warmup_coverage_accepts_90_percent(engine):
    engine.config.WARMUP_MIN_SYMBOL_COVERAGE = 0.90

    assert engine._warmup_coverage(9, 10) == (True, 9, 0.90)
    assert engine._warmup_coverage(8, 10) == (False, 9, 0.90)
    assert engine._warmup_coverage(180, 199) == (True, 180, 0.90)
