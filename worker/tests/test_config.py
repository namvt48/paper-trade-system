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


def test_supported_exchanges_are_normalized():
    s = Settings(_env_file=None, ORDERBOOK_SUPPORTED_EXCHANGES=" Binance, OKX,binance ")
    assert s.get_orderbook_exchanges() == {"binance", "okx"}


def test_runtime_requires_mds_redis_when_l2_enabled():
    s = Settings(_env_file=None, MDS_REDIS_URL="", ENABLE_ORDERBOOK_SLIPPAGE=True)
    with pytest.raises(ValueError, match="MDS_REDIS_URL"):
        s.validate_runtime()


def test_runtime_allows_no_mds_redis_when_mds_features_disabled():
    s = Settings(
        _env_file=None,
        MDS_REDIS_URL="",
        ENABLE_ORDERBOOK_SLIPPAGE=False,
        ENABLE_WORKER_TPSL_AUTO_CLOSE=False,
        ENABLE_POSITION_OWNERSHIP_MONITOR=False,
    )
    s.validate_runtime()
