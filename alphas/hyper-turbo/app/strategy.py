from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class HyperTurboSignal:
    recommend: str | None
    go_long: bool
    go_short: bool
    tp_long_signal: bool
    tp_short_signal: bool
    trend: int
    close: float
    basis: float
    dev: float
    upper: float
    lower: float
    upper_tp: float
    lower_tp: float


def _crossunder(a_prev: float, a_now: float, b_prev: float, b_now: float) -> bool:
    return bool(np.isfinite([a_prev, a_now, b_prev, b_now]).all() and a_prev >= b_prev and a_now < b_now)


def _crossover(a_prev: float, a_now: float, b_prev: float, b_now: float) -> bool:
    return bool(np.isfinite([a_prev, a_now, b_prev, b_now]).all() and a_prev <= b_prev and a_now > b_now)


def compute_hyper_turbo_signal(
    closes: list[float],
    period: int,
    tp_multiplier: float,
) -> HyperTurboSignal | None:
    if period < 2 or len(closes) < period + 1:
        return None

    close = np.asarray(closes, dtype=np.float64)
    basis = np.full(close.size, np.nan)
    dev = np.full(close.size, np.nan)
    for idx in range(period - 1, close.size):
        window = close[idx - period + 1 : idx + 1]
        basis[idx] = float(np.mean(window))
        dev[idx] = float(np.std(window, ddof=0))

    upper = basis + dev
    lower = basis - dev
    upper_tp = basis + dev * tp_multiplier
    lower_tp = basis - dev * tp_multiplier

    trend = np.zeros(close.size, dtype=np.int8)
    for idx in range(1, close.size):
        if not np.isfinite([basis[idx], upper[idx], lower[idx]]).all():
            trend[idx] = trend[idx - 1]
        elif close[idx] > basis[idx] and close[idx] > upper[idx]:
            trend[idx] = 1
        elif close[idx] < basis[idx] and close[idx] < lower[idx]:
            trend[idx] = -1
        else:
            trend[idx] = trend[idx - 1]

    prev = close.size - 2
    now = close.size - 1
    go_long = bool(trend[prev] <= 0 and trend[now] > 0)
    go_short = bool(trend[prev] >= 0 and trend[now] < 0)
    recommend = "LONG" if go_long else "SHORT" if go_short else None

    return HyperTurboSignal(
        recommend=recommend,
        go_long=go_long,
        go_short=go_short,
        tp_long_signal=_crossunder(close[prev], close[now], upper_tp[prev], upper_tp[now]),
        tp_short_signal=_crossover(close[prev], close[now], lower_tp[prev], lower_tp[now]),
        trend=int(trend[now]),
        close=float(close[now]),
        basis=float(basis[now]),
        dev=float(dev[now]),
        upper=float(upper[now]),
        lower=float(lower[now]),
        upper_tp=float(upper_tp[now]),
        lower_tp=float(lower_tp[now]),
    )
