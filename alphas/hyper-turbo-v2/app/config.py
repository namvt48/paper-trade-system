import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


BACKTEST_SYMBOLS: tuple[str, ...] = (
    "CAKEUSDT",
    "SEIUSDT",
    "BIOUSDT",
    "FIDAUSDT",
    "ORDIUSDT",
    "INJUSDT",
    "NEARUSDT",
    "OPUSDT",
    "AAVEUSDT",
    "TAOUSDT",
    "DOGEUSDT",
    "SUIUSDT",
    "BNBUSDT",
    "LINKUSDT",
    "TIAUSDT",
    "SUPERUSDT",
    "DYMUSDT",
    "IDUSDT",
    "BTCUSDT",
)


class HyperTurboV2Config(BaseConfig):
    ALPHA_ID: str = "hyper-turbo-v2"

    TF: str = "4h"
    SIGNAL_REFRESH_SEC: float = 5.0
    ENTRY_WINDOW_SEC: float = 60.0
    SIGNAL_PERIODS: str = "20,30,50"
    ATR_PERIOD: int = 14
    DAILY_MA_PERIOD: int = 50
    ATR_STOP_MULTIPLIER: float = 2.5
    CATASTROPHE_PCT: float = 0.25

    CAPITAL: float = 10_000.0
    RISK_PER_TRADE: float = 0.005
    LEVERAGE_CAP: float = 5.0
    LEVERAGE: int = 5
    MAX_CONCURRENT_POSITIONS: int = len(BACKTEST_SYMBOLS)

    SLIPPAGE_PCT: float = 0.0006
    FUNDING_RATE_8H: float = 0.0001
    FEE_PCT: float = 0.0005

    WARMUP_BARS: int = 360
    DATA_MAX_CANDLES: int = 1000
    MANAGE_INTERVAL_SEC: float = 1.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = HyperTurboV2Config()
