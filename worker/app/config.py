from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    REDIS_URL: str = "redis://localhost:6379"
    MDS_REDIS_URL: str = ""
    REDIS_STREAM: str = "paper-signals"
    CONSUMER_GROUP: str = "paper-executor"
    CONSUMER_NAME: str = "worker-1"
    VIRTUAL_TRADE_STREAM: str = "paper-shadow-trades"
    VIRTUAL_TRADE_CONSUMER_GROUP: str = "paper-shadow-ledger"
    VIRTUAL_TRADE_CONSUMER_NAME: str = "shadow-ledger-1"
    DB_PATH: str = "data/paper-trade.db"
    SLIPPAGE_PCT: float = 0.05
    DUPLICATE_POSITION_POLICY: str = "reject"
    LOG_LEVEL: str = "INFO"
    PRICE_CHECK_INTERVAL: float = 1.0
    REGISTERED_ALPHAS: str = ""
    ENABLE_WORKER_TPSL_AUTO_CLOSE: bool = False
    REDIS_READ_COUNT: int = 100
    REDIS_BLOCK_MS: int = 1000
    RECONCILE_LOG_INTERVAL_SEC: float = 300.0
    SIGNAL_RETENTION_DAYS: int = 0
    ENABLE_ORDERBOOK: bool | None = None
    ENABLE_ORDERBOOK_SLIPPAGE: bool = True
    ORDERBOOK_SUPPORTED_EXCHANGES: str = "binance"
    SLIPPAGE_RPC_TIMEOUT: float = 0.2
    MDS_REDIS_CONNECT_TIMEOUT: float = 1.0
    ORDERBOOK_SYNC_INTERVAL: float = 5.0
    TICKER_STALENESS_SEC: float = 5.0
    POSITION_SNAPSHOT_SYNC_INTERVAL_SEC: float = 5.0
    POSITION_OWNERSHIP_GRACE_SEC: float = 30.0
    POSITION_OWNERSHIP_CHECK_INTERVAL_SEC: float = 5.0
    ENABLE_POSITION_OWNERSHIP_MONITOR: bool = True
    OPEN_BOOK_PRE_SUBSCRIBE_ENABLED: bool = True
    OPEN_BOOK_READY_TIMEOUT_MS: int = 750
    OPEN_BOOK_MAX_AGE_MS: int = 500
    EXECUTION_LATENCY_MODEL_ENABLED: bool = False
    EXECUTION_LATENCY_MS: int = 50
    EXECUTION_MIN_ADVERSE_BPS: float = 0.0
    EXECUTION_SECOND_QUOTE_TIMEOUT_MS: int = 200

    # Equity snapshot collector (live equity curve)
    ENABLE_EQUITY_SNAPSHOT: bool = True
    EQUITY_SNAPSHOT_INTERVAL_SEC: float = 300.0
    EQUITY_SNAPSHOT_DB_PATH: str = "data/equity-snapshots.db"
    ALPHAS_DIR: str = "alphas"

    def get_orderbook_exchanges(self) -> set[str]:
        return {
            exchange.strip().lower()
            for exchange in self.ORDERBOOK_SUPPORTED_EXCHANGES.split(",")
            if exchange.strip()
        }

    def orderbook_enabled(self) -> bool:
        if self.ENABLE_ORDERBOOK is not None:
            return bool(self.ENABLE_ORDERBOOK)
        return bool(self.ENABLE_ORDERBOOK_SLIPPAGE)

    def validate_runtime(self) -> None:
        if not self.REDIS_URL.strip():
            raise ValueError("REDIS_URL is required")
        if not self.MDS_REDIS_URL.strip():
            raise ValueError(
                "MDS_REDIS_URL is required when orderbook, tick execution, worker TP/SL, or ownership monitor is enabled"
            )


settings = Settings()
