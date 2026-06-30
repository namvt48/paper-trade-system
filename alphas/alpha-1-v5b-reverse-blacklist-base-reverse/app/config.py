import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class Alpha1V5bReverseConfig(BaseConfig):
    ALPHA_ID: str = "alpha-1-v5b-reverse-blacklist-base-reverse"

    TF: str = "15m"
    OFFSET_CANDLE_SEC: float = 5.0

    # Core indicators — mirror backtest defaults
    SMA_LEN: int = 50
    ATR_LEN: int = 200
    POC_LEN: int = 30
    NORM_WINDOW: int = 252
    THRESHOLD: float = 0.15

    # Adaptive ATR trailing stop — multiplier range based on trend strength.
    # Strong trend (acol near ±1) → TRAIL_ATR_MIN (tighter, ride the trend)
    # Weak trend (acol near 0)   → TRAIL_ATR_MAX (wider, avoid noise)
    TRAIL_ATR_MIN: float = 0.45
    TRAIL_ATR_MAX: float = 0.55
    TP_RATIO: float = 2.0           # R:R 2:1 (reduced from 3 for higher TP hit rate on M15)
    POC_FILTER_PCT: float = 0.02    # 2% deviation from median for entry / CUT

    # Dynamic sizing — mirrors backtest_v5 scaling + Kelly WR multiplier
    CAPITAL: float = 10_000.0       # starting equity (tracks running P&L)
    INVEST_PER_TRADE: float = 100.0
    MIN_INVEST: float = 50.0
    SCALE_FACTOR: float = 0.30      # 30% of trade PnL adjusts next size

    # Kelly sizing — rolling win-rate multiplier applied on top of SCALE_FACTOR sizing.
    # multiplier = clamp(wr / KELLY_BASE_WR, 0.5, 2.0)
    KELLY_LOOKBACK: int = 20        # rolling window of recent trades for WR
    KELLY_BASE_WR: float = 0.5      # WR at which multiplier = 1 (no adjustment)

    # Trade duration guards
    MAX_TRADE_BARS: int = 500       # force-close after N candles (TIME exit)
    MIN_HOLD_BARS: int = 4          # skip SL/TP/CUT checks for first N bars after entry

    LEVERAGE: int = 10
    MAX_CONCURRENT_POSITIONS: int = 50

    WARMUP_BARS: int = 400

    LEVERAGE_FILE: str = "data/binance_futures_leverage.json"
    BLACKLIST_FILE: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = Alpha1V5bReverseConfig()
