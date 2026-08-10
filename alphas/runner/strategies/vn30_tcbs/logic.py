from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Alpha19Decision:
    side: int
    condition: int
    cut_loss: float | None
    uo: float | None
    stochastic_rank: float | None


def alpha19_decision(
    highs: list[float] | tuple[float, ...],
    lows: list[float] | tuple[float, ...],
    closes: list[float] | tuple[float, ...],
    *,
    current_side: int = 0,
    cut_loss: float | None = None,
    window: int = 60,
    window1: int = 10,
    window2: int = 40,
    threshold: float = 90.0,
) -> Alpha19Decision:
    """Evaluate Alpha 19 on the latest bar using the artifact's exact staging."""

    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("high, low, and close series must have equal lengths")
    minimum = max(window, window1, window2) + window + window + 2
    if len(closes) < minimum:
        return Alpha19Decision(current_side, 0, cut_loss, None, None)

    bp = [
        float(closes[index]) - min(float(lows[index]), float(closes[index - 1]))
        for index in range(1, len(closes))
    ]
    tr = [
        max(float(highs[index]), float(closes[index - 1]))
        - min(float(lows[index]), float(closes[index - 1]))
        for index in range(1, len(closes))
    ]

    uo_values: list[float] = []
    diff_values: list[float] = []
    latest_uo: float | None = None
    latest_diff: float | None = None
    first_index = max(window, window1, window2)
    for close_index in range(first_index, len(closes)):
        bp_end = close_index
        tr1 = sum(tr[bp_end - window1 : bp_end])
        tr2 = sum(tr[bp_end - window2 : bp_end])
        if tr1 <= 0 or tr2 <= 0:
            continue
        bp1 = sum(bp[bp_end - window1 : bp_end])
        bp2 = sum(bp[bp_end - window2 : bp_end])
        uo = 100.0 * (6.0 * (bp1 / tr1) + (bp2 / tr2)) / 7.0
        uo_values.append(uo)
        if len(uo_values) <= window:
            continue
        uo_window = uo_values[-window:]
        spread = max(uo_window) - min(uo_window)
        if spread <= 0:
            continue
        diff = (uo - min(uo_window)) / spread
        diff_values.append(diff)
        if close_index == len(closes) - 1:
            latest_uo = uo
            latest_diff = diff

    if latest_uo is None or latest_diff is None or len(diff_values) <= window:
        return Alpha19Decision(current_side, 0, cut_loss, latest_uo, latest_diff)

    diff_window = diff_values[-window:]
    condition = 0
    side = current_side
    active_cut_loss = cut_loss
    latest_low = float(lows[-1])
    latest_high = float(highs[-1])
    if (
        latest_diff >= float(np.percentile(diff_window, threshold))
        and latest_uo > 50.0
        and side <= 0
    ):
        condition = 1
        side = 1
        active_cut_loss = (math.floor(latest_low / 10.0) + 1.0) * 10.0
    elif (
        latest_diff < float(np.percentile(diff_window, 100.0 - threshold))
        and latest_uo < 50.0
        and side >= 0
    ):
        condition = -1
        side = -1
        active_cut_loss = math.floor(latest_high / 10.0) * 10.0
    elif side > 0 and active_cut_loss is not None and latest_low > active_cut_loss:
        side = 0
        active_cut_loss = None
    elif side < 0 and active_cut_loss is not None and latest_high < active_cut_loss:
        side = 0
        active_cut_loss = None

    if side > 0 and active_cut_loss is not None:
        active_cut_loss = max(
            math.floor(latest_low / 10.0) * 10.0,
            active_cut_loss,
        )
    elif side < 0 and active_cut_loss is not None:
        active_cut_loss = min(
            (math.floor(latest_high / 10.0) + 1.0) * 10.0,
            active_cut_loss,
        )
    return Alpha19Decision(side, condition, active_cut_loss, latest_uo, latest_diff)


class Alpha21AlmaCross:
    """ALMA band-cross state with an ephemeral per-bar trigger."""

    def __init__(self, period: int = 14, sigma: float = 8.0, threshold_bps: float = 25.0) -> None:
        self.period = int(period)
        if self.period < 2:
            raise ValueError("ALMA period must be at least 2")
        if sigma <= 0:
            raise ValueError("ALMA sigma must be positive")
        self.threshold_bps = float(threshold_bps)
        midpoint = self.period / 2.0
        self._weights = tuple(
            math.exp(-((index - midpoint) ** 2) / (2.0 * sigma**2))
            for index in range(1, self.period + 1)
        )
        self.closes: list[float] = []
        self.side = 0
        self.condition = 0
        self.last_alma: float | None = None

    def force_flat(self) -> None:
        self.side = 0
        self.condition = 0

    def on_bar(self, close: float) -> int:
        self.closes.append(float(close))
        self.condition = 0
        if len(self.closes) < self.period:
            return self.side
        window = self.closes[-self.period :]
        alma = sum(weight * value for weight, value in zip(self._weights, window)) / sum(self._weights)
        self.last_alma = alma
        band = self.threshold_bps / 10_000.0
        if close > alma * (1.0 + band):
            self.condition = 1
        elif close < alma * (1.0 - band):
            self.condition = -1

        if self.side != 1 and self.condition == 1:
            self.side = 1
        elif self.side != -1 and self.condition == -1:
            self.side = -1
        return self.side
