import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class Alpha1BangocConfig(BaseConfig):
    ALPHA_ID: str = "alpha-1-bangoc"

    TF: str = "15m"
    SYMBOL: str = "BTCUSDT"
    OFFSET_CANDLE_SEC: float = 5.0

    INDI1_SMA_LEN: int = 85
    INDI1_NORM_WINDOW: int = 500
    INDI1_THRESHOLD: float = 0.1

    INDI2_LOOKBACK: int = 85
    INDI2_PERCENTILE: float = 65.0

    INVEST_PER_TRADE: float = 1_000.0
    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 1
    WARMUP_BARS: int = 700
    DATA_MAX_CANDLES: int = 1000
    MANAGE_INTERVAL_SEC: float = 60.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = Alpha1BangocConfig()
