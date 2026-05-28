from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379"
    EXCHANGE: str = "binance"
    SYMBOL_MODE: str = "auto"
    SYMBOLS: str = ""
    TIMEFRAMES: str = "1m,5m,15m,30m,1h,4h,1d"
    MAX_1M_BUFFER: int = 1500
    WS_BATCH_SIZE: int = 150
    REST_SEMAPHORE: int = 25
    RECONCILE_TFS: str = "15m,1h"
    RECONCILE_DELAY: int = 5
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"
    TICKER_BATCH_SIZE: int = 150
    SNAPSHOT_MAX_CANDLES: int = 500

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    def get_timeframes(self) -> list[str]:
        return [tf.strip() for tf in self.TIMEFRAMES.split(",") if tf.strip()]

    def get_symbols_list(self) -> list[str]:
        return [symbol.strip().upper() for symbol in self.SYMBOLS.split(",") if symbol.strip()]

    def get_reconcile_tfs(self) -> list[str]:
        return [tf.strip() for tf in self.RECONCILE_TFS.split(",") if tf.strip()]


settings = Settings()
