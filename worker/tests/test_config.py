from app.config import Settings


def test_default_settings():
    s = Settings()
    assert s.REDIS_URL == "redis://localhost:6379"
    assert s.REDIS_STREAM == "paper-signals"
    assert s.CONSUMER_GROUP == "paper-executor"
    assert s.DB_PATH == "data/paper-trade.db"
    assert s.SLIPPAGE_PCT == 0.05
    assert s.DUPLICATE_POSITION_POLICY == "reject"
