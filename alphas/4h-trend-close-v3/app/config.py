from pathlib import Path

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


ROOT = Path(__file__).resolve().parents[1]


class AlphaConfig(BaseConfig):
    ALPHA_ID: str = "4h-trend-close-v3"
    TF: str = "4h"
    OFFSET_CANDLE_SEC: float = 5.0
    CAPITAL: float = 10_000.0
    WARMUP_BARS: int = 540
    DATA_MAX_CANDLES: int = 540
    MAX_CONCURRENT_POSITIONS: int = 180
    SPEC_FILE: str = str(ROOT / "spec.json")
    BLACKLIST_FILE: str = str(ROOT / "blacklist.txt")
    WHITELIST_FILE: str = str(ROOT / "whitelist.txt")

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")


settings = AlphaConfig()
