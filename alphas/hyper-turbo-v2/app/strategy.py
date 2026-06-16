from dataclasses import dataclass

import numpy as np


DAY_MS = 86_400_000
H4_MS = 14_400_000


@dataclass(frozen=True)
class HyperTurboSignal:
    recommend: str | None
    go_long: bool
    go_short: bool
    period_trends: tuple[int, ...]
    period_votes: tuple[int, ...]
    close: float
    basis: float
    dev: float
    upper: float
    lower: float
    atr: float
    risk_atr: float
    htf_ma: float
    atr_rising: bool
    htf_pass: bool


def _rolling_sma(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(values.size, np.nan)
    for idx in range(period - 1, values.size):
        out[idx] = float(np.mean(values[idx - period + 1 : idx + 1]))
    return out


def _trend_for_period(close: np.ndarray, period: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    basis = _rolling_sma(close, period)
    dev = np.full(close.size, np.nan)
    for idx in range(period - 1, close.size):
        dev[idx] = float(np.std(close[idx - period + 1 : idx + 1], ddof=0))

    trend = np.zeros(close.size, dtype=np.int8)
    upper = basis + dev
    lower = basis - dev
    for idx in range(1, close.size):
        if not np.isfinite([basis[idx], upper[idx], lower[idx]]).all():
            trend[idx] = trend[idx - 1]
        elif close[idx] > basis[idx] and close[idx] > upper[idx]:
            trend[idx] = 1
        elif close[idx] < basis[idx] and close[idx] < lower[idx]:
            trend[idx] = -1
        else:
            trend[idx] = trend[idx - 1]
    return trend, basis, dev


def _atr_sma(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    tr = np.full(close.size, np.nan)
    for idx in range(1, close.size):
        tr[idx] = max(
            high[idx] - low[idx],
            abs(high[idx] - close[idx - 1]),
            abs(low[idx] - close[idx - 1]),
        )
    return _rolling_sma(tr, period)


def _completed_daily_ma(
    closes: np.ndarray,
    open_times: list[int],
    signal_open_time: int,
    period: int,
) -> float | None:
    completed_through_day = (signal_open_time + H4_MS) // DAY_MS
    completed_daily_closes: dict[int, float] = {}
    for candle_time, close in zip(open_times, closes, strict=True):
        day = int(candle_time) // DAY_MS
        if day < completed_through_day:
            completed_daily_closes[day] = float(close)

    ordered = [completed_daily_closes[day] for day in sorted(completed_daily_closes)]
    if len(ordered) < period:
        return None
    return float(np.mean(ordered[-period:]))


def compute_hyper_turbo_signal(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    open_times: list[int],
    periods: tuple[int, ...] = (20, 30, 50),
    atr_period: int = 14,
    daily_ma_period: int = 50,
) -> HyperTurboSignal | None:
    """Compute the v2 signal from closed H4 candles only.

    Period crossover votes are averaged into one executable recommendation so
    the paper-trade worker can keep its one-position-per-symbol contract.
    """
    if not periods or atr_period < 2:
        return None
    size = len(closes)
    if size < max(max(periods) + 1, atr_period + 2):
        return None
    if not (len(highs) == len(lows) == len(open_times) == size):
        return None

    close = np.asarray(closes, dtype=np.float64)
    high = np.asarray(highs, dtype=np.float64)
    low = np.asarray(lows, dtype=np.float64)
    atr = _atr_sma(high, low, close, atr_period)
    if not np.isfinite([atr[-2], atr[-1]]).all() or atr[-2] <= 0:
        return None

    htf_ma = _completed_daily_ma(close, open_times, open_times[-1], daily_ma_period)
    if htf_ma is None:
        return None

    trends: list[int] = []
    votes: list[int] = []
    bases: list[float] = []
    devs: list[float] = []
    for period in periods:
        trend, basis, dev = _trend_for_period(close, period)
        if not np.isfinite([basis[-1], dev[-1]]).all():
            return None
        go_long = trend[-2] <= 0 and trend[-1] > 0
        go_short = trend[-2] >= 0 and trend[-1] < 0
        trends.append(int(trend[-1]))
        votes.append(1 if go_long else -1 if go_short else 0)
        bases.append(float(basis[-1]))
        devs.append(float(dev[-1]))

    vote_sum = sum(votes)
    raw_long = vote_sum > 0
    raw_short = vote_sum < 0
    atr_rising = bool(atr[-1] > atr[-2])
    long_htf_pass = bool(close[-1] > htf_ma)
    short_htf_pass = bool(close[-1] < htf_ma)
    gated_long = bool(raw_long and long_htf_pass and atr_rising)
    gated_short = bool(raw_short and short_htf_pass and atr_rising)
    recommend = "LONG" if gated_long else "SHORT" if gated_short else None

    basis_avg = float(np.mean(bases))
    dev_avg = float(np.mean(devs))
    return HyperTurboSignal(
        recommend=recommend,
        go_long=raw_long,
        go_short=raw_short,
        period_trends=tuple(trends),
        period_votes=tuple(votes),
        close=float(close[-1]),
        basis=basis_avg,
        dev=dev_avg,
        upper=basis_avg + dev_avg,
        lower=basis_avg - dev_avg,
        atr=float(atr[-1]),
        risk_atr=float(atr[-2]),
        htf_ma=htf_ma,
        atr_rising=atr_rising,
        htf_pass=long_htf_pass if raw_long else short_htf_pass if raw_short else False,
    )
