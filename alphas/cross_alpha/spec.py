from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AlphaSpec:
    alpha_id: str
    timeframe: str
    signal: str
    params: dict
    universe_size: int
    universe_mode: str
    rebalance_bars: int
    exec_lag: int
    vol_lookback: int
    ppy: int
    long_threshold: float | None
    short_threshold: float | None
    target_vol: float = 0.10
    max_leverage: float = 3.0
    fee_bps: float = 7.0
    construction: str = "rank"  # "rank" | "winsor_cont"
    winsor_k: float = 3.0  # clip threshold for winsor_cont

    @classmethod
    def load(cls, path: str | Path) -> "AlphaSpec":
        with open(path, encoding="utf-8") as fh:
            return cls(**json.load(fh))

    @property
    def required_bars(self) -> int:
        p = self.params
        signal = self.signal
        if signal in {"zscore", "absolute_zscore"}:
            return int(p["window"])
        if signal == "momentum":
            return int(p["window"]) + 1
        if signal == "decay_zscore":
            return int(p["z_window"]) + int(p["decay"]) - 1
        if signal == "blend_zscore_skew":
            return max(int(p["z_window"]), int(p["skew_window"]) + 1)
        if signal == "blend_momvol_skew":
            return max(int(p["momentum_window"]) + 1, int(p["std_window"]) + 1, int(p["skew_window"]) + 1)
        if signal == "blend_zscore_meanret":
            return max(int(p["z_window"]), int(p["mean_window"]) + 1)
        if signal == "blend_zscore_momvol":
            return max(int(p["z_window"]), int(p["momentum_window"]) + 1, int(p["std_window"]) + 1)
        if signal == "blend_zscore_decayz":
            return max(int(p["first_z_window"]), int(p["second_z_window"]) + int(p["decay"]) - 1)
        if signal == "blend_decayz_meanret":
            return max(int(p["z_window"]) + int(p["decay"]) - 1, int(p["mean_window"]) + 1)
        if signal == "blend_meanret_range":
            return max(int(p["mean_window"]) + 1, int(p["range_window"]))
        if signal == "blend_zscore_volume_zscore":
            return max(int(p["close_window"]), int(p["volume_window"]))
        if signal == "blend_decayz_volume_zscore":
            return max(int(p["close_window"]) + int(p["decay"]) - 1, int(p["volume_window"]))
        if signal == "absolute_breakout":
            return max(int(p["long_window"]), int(p["short_window"]))
        if signal == "breakout":
            return int(p["window"])
        if signal == "breakout_hl":
            return int(p["window"])
        if signal == "blend_zscore_range":
            return max(int(p["z_window"]), int(p["range_window"]))
        if signal == "amihud":
            return int(p["window"]) + 1
        raise ValueError(f"Unsupported signal: {signal}")
