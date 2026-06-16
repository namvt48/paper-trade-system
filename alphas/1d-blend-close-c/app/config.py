from pathlib import Path

from pydantic_settings import SettingsConfigDict

from base.config import BaseConfig


ROOT = Path(__file__).resolve().parents[1]


class AlphaConfig(BaseConfig):
    ALPHA_ID: str = "1d-blend-close-c"
    TF: str = "1d"
    OFFSET_CANDLE_SEC: float = 5.0
    CAPITAL: float = 10_000.0
    WARMUP_BARS: int = 65
    DATA_MAX_CANDLES: int = 65
    MAX_CONCURRENT_POSITIONS: int = 180
    SPEC_FILE: str = str(ROOT / "spec.json")
    UNIVERSE_FILE: str = str(ROOT / "data" / "universe.json")
    BLACKLIST_FILE: str = str(ROOT / "blacklist.txt")

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")


settings = AlphaConfig()
