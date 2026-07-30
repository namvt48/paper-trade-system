from pathlib import Path

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


ROOT = Path(__file__).resolve().parents[1]


class AlphaConfig(BaseConfig):
    ALPHA_ID: str = "ensemble-1d"
    TF: str = "1d"
    OFFSET_CANDLE_SEC: float = 5.0
    CAPITAL: float = 10_000.0
    # max(member required_bars: 39, 60, 20, 23) + ema_smooth(5) - 1 = 64
    WARMUP_BARS: int = 64
    DATA_MAX_CANDLES: int = 64
    MAX_CONCURRENT_POSITIONS: int = 180
    SPEC_FILE: str = str(ROOT / "spec.json")
    BLACKLIST_FILE: str = str(ROOT / "blacklist.txt")
    WHITELIST_FILE: str = str(ROOT / "whitelist.txt")

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")


settings = AlphaConfig()
