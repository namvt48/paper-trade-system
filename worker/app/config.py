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
    SLIPPAGE_PCT: float = 0.05
    DUPLICATE_POSITION_POLICY: str = "reject"
    LOG_LEVEL: str = "INFO"
    PRICE_CHECK_INTERVAL: float = 1.0
    LOG_DIR: str = "logs"
    REGISTERED_ALPHAS: str = ""


settings = Settings()
