import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class Alpha1FixedConfig(BaseConfig):
    ALPHA_ID: str = "alpha-1-fixed"

    TF: str = "15m"
    OFFSET_CANDLE_SEC: float = 5.0

    # Indicator params — mirror backtest_v5 defaults exactly
    SMA_LEN: int = 50
    ATR_LEN: int = 200
    POC_LEN: int = 30
    NORM_WINDOW: int = 252
    THRESHOLD: float = 0.15

    # Risk params — mirror backtest_v5 defaults
    TRAIL_ATR_MULT: float = 0.5
    TP_RATIO: float = 3.0
    POC_FILTER_PCT: float = 0.02   # 2% deviation from median for entry/cut

    # Position sizing
    INVEST_PER_TRADE: float = 1000.0
    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50

    # Need norm_window + sma_len + margin bars to compute acol
    WARMUP_BARS: int = 400

    # JSON file: [{"symbol": "BTCUSDT", "max_leverage": 125}, ...]
    # Used to look up per-symbol max leverage; falls back to LEVERAGE if symbol not found.
    LEVERAGE_FILE: str = "data/binance_futures_leverage.json"

    # Path to a text file: one Binance symbol per line; blank lines and # comments ignored.
    # Merged with SYMBOL_BLACKLIST env var at startup.
    BLACKLIST_FILE: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = Alpha1FixedConfig()
