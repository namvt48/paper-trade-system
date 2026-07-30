from pathlib import Path

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


ROOT = Path(__file__).resolve().parents[1]


class AlphaConfig(BaseConfig):
    ALPHA_ID: str = "1h-decay-close-v3"
    TF: str = "1h"
    OFFSET_CANDLE_SEC: float = 5.0
    CAPITAL: float = 10_000.0
    WARMUP_BARS: int = 3119
    DATA_MAX_CANDLES: int = 3119
    MAX_CONCURRENT_POSITIONS: int = 180
    SPEC_FILE: str = str(ROOT / "spec.json")
    BLACKLIST_FILE: str = str(ROOT / "blacklist.txt")
    WHITELIST_FILE: str = str(ROOT / "whitelist.txt")

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")


settings = AlphaConfig()
