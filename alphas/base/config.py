from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseConfig(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379"
    REDIS_STREAM: str = "paper-signals"
    ALPHA_ID: str = ""
    INVEST_PER_TRADE: float = 100.0
    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"
    EXCHANGE: str = "binance"
    FEE_PCT: float = 0.0005
    DATA_CHANNELS: str = ""
    DATA_MAX_CANDLES: int = 1000
    INITIAL_DATA_TIMEOUT_SEC: float = 30.0
    WARMUP_BARS: int = 50
    SYMBOL_BLACKLIST: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )
