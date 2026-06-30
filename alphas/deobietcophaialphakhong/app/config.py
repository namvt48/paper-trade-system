import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


class DeoBietCoPhaiAlphaKhongConfig(BaseConfig):
    """Song Than — zone-based mean-reversion on Futures.

    Params mirror docs/alphas/song_than.md defaults.
    """

    ALPHA_ID: str = "deobietcophaialphakhong"

    # Timeframe: zone calculation runs on 15m bars.
    TF: str = "15m"
    ZONE_TF: str = "15m"
    OFFSET_CANDLE_SEC: float = 5.0

    # ── Zone calculation (section 2) ──────────────────────────────────────────
    SWING_LENGTH: int = 50            # L — pivot window in bars on ZONE_TF

    # ── Sizing ────────────────────────────────────────────────────────────────
    # Margin per trade x leverage = notional. Default: $100 x 50 = $5000.
    INVEST_PER_TRADE: float = 100.0
    LEVERAGE: int = 50
    SL_LONG_PCT: float = 0.007        # 0.7%
    SL_SHORT_PCT: float = 0.009       # 0.9%
    TP_PCT: float = 0.02              # 2.0%

    # ── Trailing stop — 2 milestones (section 4.2) ────────────────────────────
    TRAIL_M1_PCT: float = 0.0125      # 1.25%  → milestone 1
    TRAIL_M1_SL_PCT: float = 0.001    # 0.1%   SL offset at milestone 1
    TRAIL_M2_PCT: float = 0.0185      # 1.85%  → milestone 2
    TRAIL_M2_SL_PCT: float = 0.005    # 0.5%   SL offset at milestone 2

    # ── Reverse entry (section 5) ─────────────────────────────────────────────
    REVERSE_SL_COUNT: int = 3         # consecutive SL count to trigger reverse
    REVERSE_SL_PCT: float = 0.0175    # 1.75%
    REVERSE_TP_PCT: float = 0.025     # 2.5%

    # ── Capital ───────────────────────────────────────────────────────────────
    CAPITAL: float = 10_000.0
    MAX_CONCURRENT_POSITIONS: int = 50
    WARMUP_BARS: int = 800            # L=50 swing + trailing buffer

    BLACKLIST_FILE: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = DeoBietCoPhaiAlphaKhongConfig()
