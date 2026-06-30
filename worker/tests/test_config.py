import pytest

from app.config import Settings


def test_default_settings():
    s = Settings(_env_file=None)
    assert s.REDIS_URL == "redis://localhost:6379"
    assert s.MDS_REDIS_URL == ""
    assert s.REDIS_STREAM == "paper-signals"
    assert s.CONSUMER_GROUP == "paper-executor"
    assert s.DB_PATH == "data/paper-trade.db"
    assert s.SLIPPAGE_PCT == 0.05
    assert s.MDS_REDIS_CONNECT_TIMEOUT == 1.0
    assert s.DUPLICATE_POSITION_POLICY == "reject"
    assert s.orderbook_enabled() is True


def test_supported_exchanges_are_normalized():
    s = Settings(_env_file=None, ORDERBOOK_SUPPORTED_EXCHANGES=" Binance, OKX,binance ")
    assert s.get_orderbook_exchanges() == {"binance", "okx"}


def test_runtime_requires_mds_redis_when_l2_enabled():
    s = Settings(_env_file=None, MDS_REDIS_URL="", ENABLE_ORDERBOOK=True)
    with pytest.raises(ValueError, match="MDS_REDIS_URL"):
        s.validate_runtime()


def test_runtime_requires_mds_redis_when_orderbook_disabled_for_tick_execution():
    s = Settings(
        _env_file=None,
        MDS_REDIS_URL="",
        ENABLE_ORDERBOOK=False,
        ENABLE_WORKER_TPSL_AUTO_CLOSE=False,
        ENABLE_POSITION_OWNERSHIP_MONITOR=False,
    )
    with pytest.raises(ValueError, match="MDS_REDIS_URL"):
        s.validate_runtime()


def test_enable_orderbook_overrides_legacy_slippage_flag():
    s = Settings(_env_file=None, ENABLE_ORDERBOOK=False, ENABLE_ORDERBOOK_SLIPPAGE=True)
    assert s.orderbook_enabled() is False
