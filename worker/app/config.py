from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    REDIS_URL: str = "redis://localhost:6379"
    REDIS_STREAM: str = "paper-signals"
    CONSUMER_GROUP: str = "paper-executor"
    CONSUMER_NAME: str = "worker-1"
    DB_PATH: str = "data/paper-trade.db"
    SLIPPAGE_PCT: float = 0.5
    DUPLICATE_POSITION_POLICY: str = "reject"
    LOG_LEVEL: str = "INFO"
    PRICE_CHECK_INTERVAL: float = 1.0
    LOG_DIR: str = "logs"
    REGISTERED_ALPHAS: str = ""
    ENABLE_WORKER_TPSL_AUTO_CLOSE: bool = False
    REDIS_READ_COUNT: int = 100
    REDIS_BLOCK_MS: int = 1000
    SIGNAL_RETENTION_DAYS: int = 0
    ENABLE_ORDERBOOK_SLIPPAGE: bool = True
    ORDERBOOK_EXCHANGE: str = "binance"
    SLIPPAGE_RPC_TIMEOUT: float = 0.2
    ORDERBOOK_SYNC_INTERVAL: float = 5.0


settings = Settings()
