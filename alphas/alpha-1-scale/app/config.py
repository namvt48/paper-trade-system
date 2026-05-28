import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class Alpha1ScaleConfig(BaseConfig):
    ALPHA_ID: str = "alpha-1-scale"

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

    # Dynamic position sizing — mirrors backtest_v5 scaling logic exactly:
    #   cur_size += SCALE_FACTOR * net_pnl
    #   cur_size = clamp(cur_size, MIN_INVEST, SCALE_FACTOR * cur_equity)
    CAPITAL: float = 10_000.0          # starting equity (tracks running P&L)
    INVEST_PER_TRADE: float = 1_000.0  # initial cur_size
    MIN_INVEST: float = 500.0          # floor on cur_size
    SCALE_FACTOR: float = 0.30         # 30% of trade PnL adjusts next trade size

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


settings = Alpha1ScaleConfig()
