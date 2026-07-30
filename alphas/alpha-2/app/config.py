import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict
from base.config import BaseConfig

class Alpha2Config(BaseConfig):
    ALPHA_ID: str = "alpha-2"

    TF: str = "4h"
    HTF: str = "12h"
    OFFSET_CANDLE_SEC: float = 5.0

    SMA_LEN: int = 85
    DIFF_LAG: int = 5
    DENOM_LEN: int = 500
    TREND_UP: float = 0.1
    TREND_DN: float = -0.1

    OPI_LEN: int = 85
    OPI_PCT: float = 65.0

    USE_VT: bool = True
    TARGET_VOL: float = 0.30
    VT_DAYS: int = 30
    VT_CAP: float = 1.0

    CAPITAL: float = 10_000.0
    INVEST_PER_TRADE: float = 1_000.0
    MIN_INVEST: float = 500.0

    # Trade duration guards
    MAX_TRADE_BARS: int = 500
    MIN_HOLD_BARS: int = 0

    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50
    WARMUP_BARS: int = 900
    FEE_PCT: float = 0.0005 # Default

    LEVERAGE_FILE: str = "data/binance_futures_leverage.json"
    BLACKLIST_FILE: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

settings = Alpha2Config()
