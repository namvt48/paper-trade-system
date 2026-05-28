import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class ADXTrendFollowConfig(BaseConfig):
    ALPHA_ID: str = "adx-trend-follow"

    TF: str = "15m"
    OFFSET_CANDLE_SEC: float = 5.0

    ADX_PERIOD: int = 7
    ADX_THRESHOLD: float = 50.0

    VOL_LOOKBACK: int = 4
    PRICE_LOOKBACK: int = 4
    BTC_DIR_LOOKBACK: int = 2
    VOL_SPIKE_MIN: float = 2.0
    PRICE_MOVE_MIN: float = 0.008
    PRICE_MOVE_MAX: float = 0.200

    INITIAL_SL_PCT: float = 0.005
    BE_TRIGGER_PCT: float = 0.003
    TRAIL_DIST_PCT: float = 0.005
    TP_CAP_PCT: float = 0.030
    MAX_HOLD_CANDLES: int = 40

    INVEST_PER_TRADE: float = 100.0
    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = ADXTrendFollowConfig()
