import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class Alpha1Q2Config(BaseConfig):
    ALPHA_ID: str = "alpha-1-q2"

    TF: str = "15m"
    OFFSET_CANDLE_SEC: float = 5.0

    # Indicator params (identical to v5b)
    SMA_LEN: int = 50
    ATR_LEN: int = 200
    POC_LEN: int = 30
    NORM_WINDOW: int = 252
    THRESHOLD: float = 0.15

    # Q2 Mid Cap trading params
    # Rank 200-400 by 24h futures quote volume
    VOLUME_RANK_START: int = 200
    VOLUME_RANK_END: int = 400

    TRAIL_ATR_MIN: float = 0.60
    TRAIL_ATR_MAX: float = 0.80
    TP_RATIO: float = 2.5
    POC_FILTER_PCT: float = 0.025  # slightly wider — mid cap more volatile

    CAPITAL: float = 10_000.0
    INVEST_PER_TRADE: float = 1_000.0
    MIN_INVEST: float = 500.0
    SCALE_FACTOR: float = 0.30

    KELLY_LOOKBACK: int = 20
    KELLY_BASE_WR: float = 0.5

    MAX_TRADE_BARS: int = 500
    MIN_HOLD_BARS: int = 6

    LEVERAGE: int = 5
    MAX_CONCURRENT_POSITIONS: int = 200

    WARMUP_BARS: int = 400

    LEVERAGE_FILE: str = "data/binance_futures_leverage.json"
    BLACKLIST_FILE: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = Alpha1Q2Config()
