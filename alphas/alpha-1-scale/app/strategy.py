from typing import Optional

from app.config import settings


def _calc_sma(vals: list[float], p: int) -> list[Optional[float]]:
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    s = 0.0
    for i in range(n):
        s += vals[i]
        if i >= p:
            s -= vals[i - p]
        if i >= p - 1:
            out[i] = s / p
    return out


def _calc_atr(hi: list[float], lo: list[float], cl: list[float], p: int) -> list[Optional[float]]:
    n = len(cl)
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
    out: list[Optional[float]] = [None] * n
    s = 0.0
    for i in range(1, n):
        s += trs[i]
        if i > p:
            s -= trs[i - p]
        if i >= p:
            out[i] = s / p
    return out


def _calc_median(vals: list[float], p: int) -> list[Optional[float]]:
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    for i in range(p - 1, n):
        w = sorted(vals[i - p + 1: i + 1])
        m = p // 2
        out[i] = (w[m - 1] + w[m]) / 2 if p % 2 == 0 else w[m]
    return out


def reconstruct_trend_state(
    close_list: list[float],
    sma_len: int,
    norm_window: int,
    threshold: float,
) -> Optional[bool]:
    """Replay acol crossing logic over all available bars to derive current trend state.

    Called once after warmup so the engine starts with a correct trend state
    instead of waiting for the first live crossing to happen.
    """
    n = len(close_list)
    if n < norm_window + sma_len + 10:
        return None

    avg = _calc_sma(close_list, sma_len)

    adiff: list[Optional[float]] = [None] * n
    for i in range(5, n):
        if avg[i] is not None and avg[i - 5] is not None:
            adiff[i] = avg[i] - avg[i - 5]  # type: ignore[operator]

    acol: list[Optional[float]] = [None] * n
    for i in range(norm_window, n):
        ds = [d for d in adiff[i - norm_window + 1: i + 1] if d is not None]
        if ds:
            abs_max = max(abs(v) for v in ds)
            if abs_max > 1e-12 and adiff[i] is not None:
                acol[i] = adiff[i] / abs_max  # type: ignore[operator]

    trend: Optional[bool] = None
    for i in range(1, n):
        ac = acol[i]
        acp = acol[i - 1]
        if ac is None or acp is None:
            continue
        if acp <= threshold and ac > threshold and trend is not True:
            trend = True
        if acp >= -threshold and ac < -threshold and trend is True:
            trend = False

    return trend


def compute_alpha_v5_indicators(
    close_list: list[float],
    high_list: list[float],
    low_list: list[float],
) -> Optional[dict]:
    """Compute normalized SMA momentum signal with ATR and POC (median).

    Mirrors backtest_v5 logic exactly:
    - SMA(sma_len) on close
    - adiff = sma[i] - sma[i-5]
    - acol = adiff / rolling_abs_max(adiff, norm_window) — range [-1, 1]
    - ATR(atr_len), Median/POC(poc_len)

    Returns None when there is not enough data to produce valid indicators.
    Uses adjacent pair (acol[-2], acol[-1]) for crossing detection — same as
    the per-bar loop in the backtest.
    """
    sma_len = settings.SMA_LEN
    atr_len = settings.ATR_LEN
    poc_len = settings.POC_LEN
    norm_window = settings.NORM_WINDOW

    n = len(close_list)
    min_bars = norm_window + sma_len + 10
    if n < min_bars:
        return None

    avg = _calc_sma(close_list, sma_len)
    atr_v = _calc_atr(high_list, low_list, close_list, atr_len)
    poc = _calc_median(close_list, poc_len)

    adiff: list[Optional[float]] = [None] * n
    for i in range(5, n):
        if avg[i] is not None and avg[i - 5] is not None:
            adiff[i] = avg[i] - avg[i - 5]  # type: ignore[operator]

    acol: list[Optional[float]] = [None] * n
    for i in range(norm_window, n):
        ds = [d for d in adiff[i - norm_window + 1: i + 1] if d is not None]
        if ds:
            abs_max = max(abs(v) for v in ds)
            if abs_max > 1e-12 and adiff[i] is not None:
                acol[i] = adiff[i] / abs_max  # type: ignore[operator]

    # Use strict adjacent pair to mirror per-bar crossing detection
    ac = acol[-1]
    acp = acol[-2]
    ai = atr_v[-1]
    pi = poc[-1]

    if None in (ac, acp, ai, pi) or ai <= 0:  # type: ignore[operator]
        return None

    return {
        "acol": float(ac),       # type: ignore[arg-type]
        "acol_prev": float(acp), # type: ignore[arg-type]
        "atr": float(ai),        # type: ignore[arg-type]
        "poc": float(pi),        # type: ignore[arg-type]
        "close": float(close_list[-1]),
        "high": float(high_list[-1]),
        "low": float(low_list[-1]),
    }
