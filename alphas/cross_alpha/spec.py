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
    vol_lookback: int
    ppy: int
    long_threshold: float | None
    short_threshold: float | None
    target_vol: float = 0.10
    max_leverage: float = 3.0
    fee_bps: float = 7.0
    construction: str = "rank"  # "rank" | "winsor_cont"
    winsor_k: float = 3.0  # clip threshold for winsor_cont
    reverse: bool = False  # when True, swap LONG↔SHORT (negate all weights)
    # When True, signals publish only on the scan whose candle closes at a
    # 00:00 UTC day boundary (entries at ~00:00, never at other hours).
    publish_at_midnight_utc: bool = False
    # Count rebalance bars on candle-close boundaries without imposing a
    # midnight-only gate. Use this for cadences such as 36h that must alternate
    # between 00:00 and 12:00 UTC while remaining exactly 36 hours apart.
    rebalance_on_close: bool = False
    # When True, the runner attaches a cross-sectional funding panel
    # (panel["funding_zscore"]) before calling select_positions() -- required
    # by any signal that reads fields["funding_zscore"] (e.g. carry_momentum).
    # The zscore is computed at funding's OWN native settlement frequency
    # (params["funding_window"] settlements, e.g. 21 settlements @ 8h ~= 7d)
    # before being reindexed/ffilled onto the alpha's own (typically daily)
    # kline index -- computing it after reindexing would silently stretch the
    # window to 21 *daily* bars (~3x too long). See
    # CrossSectionalRunnerStrategy._attach_funding_panel.
    needs_funding: bool = False
    # Publish a normalized target book without emitting worker OPEN/CLOSE signals.
    book_only: bool = False
    # Ensemble-only fields (signal == "ensemble_mean"): member alpha_ids whose
    # own spec.json (each already built/tested standalone) get combined, and
    # the portfolio-overlay config (risk_parity/beta_neutralize/per_coin_cap/
    # drawdown_throttle) applied to the combined signal. See
    # cross_alpha/ensemble.py and cross_alpha/overlay.py.
    members: list[str] | None = None
    overlay: dict | None = None
    ema_smooth: int | None = None
    # Top-K + power sizing for construction="winsor_cont" (ported from the
    # standalone projects docs/1d-vwaprev-w50-top15-p20 and
    # docs/1d-vwaprev-w80-top25-p15). When top_k is set, only the top_k
    # symbols with the LARGEST (long side) / SMALLEST (short side) clipped
    # z-score per side are held, sized by sign(z)*|z|^power_p instead of
    # plain z. When top_k is None the classic full-cross-section winsor_cont
    # behavior is kept exactly (production alphas like 15m-blend-close never
    # set this field).
    top_k: int | None = None
    power_p: float = 1.0

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
            return max(
                int(p["momentum_window"]) + 1,
                int(p["std_window"]) + 1,
                int(p["skew_window"]) + 1,
            )
        if signal == "blend_zscore_meanret":
            return max(int(p["z_window"]), int(p["mean_window"]) + 1)
        if signal == "blend_zscore_momvol":
            return max(
                int(p["z_window"]),
                int(p["momentum_window"]) + 1,
                int(p["std_window"]) + 1,
            )
        if signal == "blend_zscore_decayz":
            return max(
                int(p["first_z_window"]),
                int(p["second_z_window"]) + int(p["decay"]) - 1,
            )
        if signal == "blend_decayz_meanret":
            return max(
                int(p["z_window"]) + int(p["decay"]) - 1, int(p["mean_window"]) + 1
            )
        if signal == "blend_meanret_range":
            return max(int(p["mean_window"]) + 1, int(p["range_window"]))
        if signal == "blend_zscore_volume_zscore":
            return max(int(p["close_window"]), int(p["volume_window"]))
        if signal == "blend_decayz_volume_zscore":
            return max(
                int(p["close_window"]) + int(p["decay"]) - 1, int(p["volume_window"])
            )
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
        if signal == "kaufman_trend":
            return int(p["er_window"]) + int(p["ema_span"]) - 1
        if signal == "trend_cmf_blend":
            return max(
                int(p["z_window"]), int(p["cmf_window"]) + int(p["ema_span"]) - 1
            )
        if signal == "vwap_reversion":
            return int(p["vwap_window"]) + int(p["ema_span"]) - 1
        if signal == "carry_momentum":
            # funding_window applies at funding's own native settlement
            # frequency (resolved upstream by the runner, not here) -- only
            # momentum_window and the ema_span (applied post-reindex, on the
            # alpha's own daily bars) count toward this alpha's own warmup.
            return max(int(p["momentum_window"]) + 1, int(p["ema_span"]))
        if signal == "ideal_amplitude":
            # ideal_amp itself needs window+5 valid bars before it produces
            # any output (a hard floor baked into the indicator, matching
            # its reference implementation) plus ema_span-1 more for the
            # smoothing to stabilize.
            return int(p["window"]) + 5 + int(p["ema_span"]) - 1
        if signal == "ensemble_mean":
            raise ValueError(
                f"{self.alpha_id}: ensemble_mean's required_bars depends on its members' own "
                "required_bars (load each member's spec.json and take the max) -- not "
                "computable from this spec's own params alone"
            )
        raise ValueError(f"Unsupported signal: {signal}")
