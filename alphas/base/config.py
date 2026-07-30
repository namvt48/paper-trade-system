from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseConfig(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379"
    REDIS_STREAM: str = "paper-signals"
    ALPHA_ID: str = ""
    INVEST_PER_TRADE: float = 100.0
    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50
    LOG_LEVEL: str = "INFO"
    EXCHANGE: str = "binance"
    FEE_PCT: float = 0.0005
    DATA_CHANNELS: str = ""
    DATA_MAX_CANDLES: int = 1000
    # Large universe warmups are deliberately rate-limited by MDS and may queue.
    INITIAL_DATA_TIMEOUT_SEC: float = 300.0
    WARMUP_BARS: int = 50
    WARMUP_MIN_SYMBOL_COVERAGE: float = 0.90
    SYMBOL_BLACKLIST: str = ""
    SYMBOL_WHITELIST: str = ""
    WHITELIST_FILE: str = ""
    MANAGE_INTERVAL_SEC: float = 60.0
    MDS_REDIS_URL: str = ""
    MDS_EXCHANGE: str = ""
    PRICE_ALERT_SYNC_INTERVAL_SEC: float = 5.0
    PRICE_ALERT_STALE_SEC: float = 15.0
    POSITION_RECONCILE_INTERVAL_SEC: float = 5.0
    POSITION_SNAPSHOT_MAX_AGE_SEC: float = 15.0
    ALPHA_RUNTIME_HEARTBEAT_TTL_SEC: int = 20
    POSITION_RECONCILE_STARTUP_TIMEOUT_SEC: float = 30.0
    RECONNECT_WARMUP_SKIP_IF_FRESH_SEC: float = 300.0
    RECONCILE_NO_POSITION_IS_OK: bool = True
    RECONCILE_STALE_SUSPEND_NEW_ENTRIES: bool = True
    DATA_STALE_SUSPEND_NEW_ENTRIES: bool = True
    PRICE_ALERT_SYNC_SUSPEND_NEW_ENTRIES: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )
