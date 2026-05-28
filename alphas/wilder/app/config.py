import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class WilderConfig(BaseConfig):
    ALPHA_ID: str = "wilder"

    TF: str = "1h"
    OFFSET_CANDLE_SEC: float = 5.0

    RSI_PERIOD: int = 14
    ADX_PERIOD: int = 14
    ATR_PERIOD: int = 14

    SAR_AF_INIT: float = 0.02
    SAR_AF_STEP: float = 0.02
    SAR_AF_MAX: float = 0.20

    TRENDING_THRESHOLD: float = 35.0
    RANGING_THRESHOLD: float = 25.0

    DI_GAP_MIN: float = 5.0
    RSI_OVERSOLD: float = 32.0
    RSI_OVERBOUGHT: float = 68.0

    SL_ATR_MULT: float = 2.0
    TP_ATR_MULT: float = 6.0
    TRAIL_ATR_MULT: float = 0.5

    TOP_N_COINS: int = 120

    INVEST_PER_TRADE: float = 300.0
    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = WilderConfig()
