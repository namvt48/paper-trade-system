"""Settings for short-btc-v1: short-only BTCUSDT breakdown with D1 downtrend gate
and funding/OI context-sized partial exit.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class ShortBtcV1Config(BaseConfig):
    ALPHA_ID: str = "short-btc-v1"

    TF: str = "15m"
    HTF: str = "1d"
    SYMBOL: str = "BTCUSDT"

    EMA_FAST: int = 50
    EMA_SLOW: int = 200
    RSI_LEN: int = 14
    RSI_THRESH: float = 40.0
    ATR_LEN: int = 14
    CLV_MAX: float = 0.25

    # Breakdown lookback: 48h expressed in bars of TF, used when the D1 gate is on.
    D1_GATE_LOOKBACK_HOURS: int = 48
    D1_EMA_FAST: int = 20
    D1_EMA_SLOW: int = 50
    D1_SLOPE_LOOKBACK: int = 5

    SL_ATR_MULT: float = 0.8
    TP_RATIO: float = 1.2
    MAX_HOLD_H: float = 24.0

    # Fixed reduce_fraction fallback used only if funding/OI context data is unavailable.
    REDUCE_FRACTION_DEFAULT: float = 0.5

    INVEST_PER_TRADE: float = 200.0
    LEVERAGE: int = 5
    MAX_CONCURRENT_POSITIONS: int = 1
    WARMUP_BARS: int = 500

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = ShortBtcV1Config()
