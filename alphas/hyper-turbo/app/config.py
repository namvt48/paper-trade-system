import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class HyperTurboConfig(BaseConfig):
    ALPHA_ID: str = "hyper-turbo"

    TF: str = "15m"
    SYMBOL: str = "BTCUSDT"
    SIGNAL_REFRESH_SEC: float = 60.0
    SIGNAL_PERIOD: int = 20
    TP_MULTIPLIER: float = 2.5

    INVEST_PER_TRADE: float = 100.0
    LEVERAGE: int = 30
    MAX_CONCURRENT_POSITIONS: int = 1
    WARMUP_BARS: int = 21
    DATA_MAX_CANDLES: int = 1000
    MANAGE_INTERVAL_SEC: float = 1.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = HyperTurboConfig()
