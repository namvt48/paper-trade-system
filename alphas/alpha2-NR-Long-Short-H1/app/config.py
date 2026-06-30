import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class AlphaConfig(BaseConfig):
    ALPHA_ID: str = "alpha2-NR-Long-Short-H1"
    TF: str = "1h"
    OFFSET_CANDLE_SEC: float = 5.0

    # indi1: EMA cross
    EMA_FAST: int = 12
    EMA_SLOW: int = 25

    # indi2: Hull Butterfly
    HULL_LENGTH: int = 14

    # Sizing
    CAPITAL: float = 10_000.0
    INVEST_PER_TRADE: float = 1_000.0

    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 1
    WARMUP_BARS: int = 200
    FEE_PCT: float = 0.0005

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = AlphaConfig()
