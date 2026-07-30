"""Pure indicator/signal math for short-btc-v1 — ported from
market-data-service/docs/backtest_v2.py. No I/O; all functions operate on
plain lists/scalars so they're testable without Redis or the runner.
"""

from __future__ import annotations

from bisect import bisect_right


def calc_ema(vals: list[float], p: int) -> list[float | None]:
    n = len(vals)
    out: list[float | None] = [None] * n
    if n < p:
        return out
    k = 2 / (p + 1)
    s = sum(vals[:p]) / p
    out[p - 1] = s
    for i in range(p, n):
        s = vals[i] * k + s * (1 - k)
        out[i] = s
    return out


def calc_atr(hi: list[float], lo: list[float], cl: list[float], p: int) -> list[float | None]:
    n = len(cl)
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
    out: list[float | None] = [None] * n
    if n < p + 1:
        return out
    s = sum(trs[1:p + 1]) / p
    out[p] = s
    alpha = 1.0 / p
    for i in range(p + 1, n):
        s = (trs[i] - s) * alpha + s
        out[i] = s
    return out


def calc_rsi(vals: list[float], p: int) -> list[float | None]:
    n = len(vals)
    out: list[float | None] = [None] * n
    if n <= p:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, p + 1):
        change = vals[i] - vals[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / p
    avg_loss = losses / p
    out[p] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    for i in range(p + 1, n):
        change = vals[i] - vals[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (p - 1) + gain) / p
        avg_loss = (avg_loss * (p - 1) + loss) / p
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def calc_clv(high: float, low: float, close: float) -> float:
    rng = high - low
    if rng == 0:
        return 0.0
    return (close - low) / rng


def passes_d1_downtrend(
    d1_closes: list[float],
    d1_ema_fast: list[float | None],
    d1_ema_slow: list[float | None],
    d1_open_times_ms: list[int],
    signal_time_ms: int,
    slope_lookback: int = 5,
    period_ms: int = 86_400_000,
) -> bool:
    """True when the last CLOSED daily bar at-or-before signal_time_ms is in a
    downtrend: close < ema_slow, ema_fast < ema_slow, and ema_fast sloping down
    over the last `slope_lookback` daily bars.
    """
    if not d1_open_times_ms:
        return False

    close_times = [t + period_ms for t in d1_open_times_ms]
    idx = bisect_right(close_times, signal_time_ms) - 1
    if idx < slope_lookback:
        return False
    if d1_ema_fast[idx] is None or d1_ema_slow[idx] is None or d1_ema_fast[idx - slope_lookback] is None:
        return False

    return (
        d1_closes[idx] < d1_ema_slow[idx]
        and d1_ema_fast[idx] < d1_ema_slow[idx]
        and (d1_ema_fast[idx] - d1_ema_fast[idx - slope_lookback]) < 0
    )


def compute_entry_signal(
    close_list: list[float],
    high_list: list[float],
    low_list: list[float],
    open_list: list[float],
    ema_fast: list[float | None],
    ema_slow: list[float | None],
    rsi: list[float | None],
    atr: list[float | None],
    lookback_bars: int,
    rsi_thresh: float,
    clv_max: float,
    sl_atr_mult: float,
    tp_ratio: float,
) -> dict | None:
    """Evaluates entry conditions on the latest (just-closed) bar. Live entry
    price is that bar's close — the backtest's next-bar-open is not available
    live, so this is an intentional simplification (see plan notes).
    """
    n = len(close_list)
    i = n - 1
    if i < lookback_bars:
        return None
    if ema_fast[i] is None or ema_slow[i] is None or rsi[i] is None or atr[i] is None:
        return None

    downtrend = ema_fast[i] < ema_slow[i]
    lowest_close = min(close_list[i - lookback_bars:i])
    breakdown = close_list[i] < lowest_close
    red_candle = close_list[i] < open_list[i]
    rsi_ok = rsi[i] <= rsi_thresh
    clv = calc_clv(high_list[i], low_list[i], close_list[i])
    clv_ok = clv <= clv_max

    if not (downtrend and breakdown and red_candle and rsi_ok and clv_ok):
        return None

    entry = close_list[i]
    sl = entry + sl_atr_mult * atr[i]
    tp = entry - tp_ratio * (sl - entry)
    return {"entry": entry, "sl": sl, "tp": tp, "signal_close": entry}


def read_last_at_or_before(rows: list[dict], time_field: str, ts_ms: int) -> dict | None:
    """rows must be sorted ascending by time_field (as returned by MDS's
    ContextReader). Returns the last row with time <= ts_ms, or None."""
    if not rows:
        return None
    times = [r.get(time_field, 0) for r in rows]
    idx = bisect_right(times, ts_ms) - 1
    if idx < 0:
        return None
    return rows[idx]


def read_last_completed_daily(
    rows: list[dict], time_field: str, ts_ms: int, period_ms: int = 86_400_000
) -> tuple[dict | None, dict | None]:
    """Returns (latest_completed_row, previous_row). A row's bucket closes at
    time_field + period_ms; only counts as "completed" once that close passes
    ts_ms."""
    if not rows:
        return None, None
    close_times = [r.get(time_field, 0) + period_ms for r in rows]
    idx = bisect_right(close_times, ts_ms) - 1
    if idx < 0:
        return None, None
    prev = rows[idx - 1] if idx > 0 else None
    return rows[idx], prev


def compute_context_exit_fraction(
    funding_rate: float | None,
    oi_close: float | None,
    oi_prev_close: float | None,
) -> tuple[float, dict]:
    """Sizes the reduce fraction from funding rate + daily OI change at signal
    time — ported verbatim from backtest_v2.py's _context_exit_decision.
    """
    oi_change_pct = None
    if oi_close is not None and oi_prev_close not in (None, 0):
        oi_change_pct = (oi_close - oi_prev_close) / oi_prev_close * 100.0

    funding_bad = funding_rate is not None and funding_rate <= 0
    oi_bad = oi_close is not None and oi_prev_close is not None and oi_close <= oi_prev_close
    bad_count = int(funding_bad) + int(oi_bad)

    if bad_count >= 2:
        reduce_fraction = 1.0
    elif bad_count == 1:
        reduce_fraction = 0.7
    else:
        reduce_fraction = 0.5

    return reduce_fraction, {
        "funding_at_signal": funding_rate,
        "oi_daily_at_signal": oi_close,
        "oi_daily_prev": oi_prev_close,
        "oi_daily_change_pct": oi_change_pct,
        "context_bad_count": bad_count,
    }
