from typing import Optional
import numpy as np
import pandas as pd

from app.config import settings

def compute_greenred(df: pd.DataFrame) -> list[Optional[str]]:
    close = df["close"]
    avg = close.rolling(settings.SMA_LEN).mean()
    avg_diff = avg - avg.shift(settings.DIFF_LAG)
    denom = avg_diff.rolling(settings.DENOM_LEN).max()
    avg_col = avg_diff / denom

    prev = avg_col.shift(1)
    cross_up = (prev <= settings.TREND_UP) & (avg_col > settings.TREND_UP)
    cross_dn = (prev >= settings.TREND_DN) & (avg_col < settings.TREND_DN)

    state, colors = None, []
    for cu, cd in zip(cross_up.fillna(False).values, cross_dn.fillna(False).values):
        if cu and state is not True:
            state = True
        elif cd and state is True:
            state = False
        colors.append("green" if state is True else "red" if state is False else None)
    return colors

def compute_strategy(df: pd.DataFrame) -> list[Optional[str]]:
    src = df["close"]
    low_p = (100 - settings.OPI_PCT) / 2
    up_p = 100 - low_p
    n = settings.OPI_LEN

    poc = src.rolling(n).median()
    lower = src.rolling(n).apply(lambda x: np.percentile(x, low_p), raw=True)
    upper = src.rolling(n).apply(lambda x: np.percentile(x, up_p), raw=True)

    sig_up = (src >= upper) & (poc >= poc.shift(1))
    sig_dn = (src <= lower) & (poc <= poc.shift(1))

    cur, colors = None, []
    for u, d in zip(sig_up.values, sig_dn.values):
        if u:
            cur = "green"
        elif d:
            cur = "red"
        colors.append(cur)
    return colors

def combined_state(trend_color: np.ndarray, strategy_color: np.ndarray) -> np.ndarray:
    g = (trend_color == "green") & (strategy_color == "green")
    r = (trend_color == "red") & (strategy_color == "red")
    return np.where(g, "long", np.where(r, "short", "none"))

def persistent_bias(state: np.ndarray) -> list[Optional[str]]:
    out, cur = [], None
    for st in state:
        if st == "long":
            cur = "long"
        elif st == "short":
            cur = "short"
        out.append(cur)
    return out

def tf_delta(interval: str) -> pd.Timedelta:
    v, u = int(interval[:-1]), interval[-1]
    return pd.Timedelta(hours=v) if u == "h" else pd.Timedelta(days=v)

def attach_htf_bias(df_base: pd.DataFrame, df_htf: pd.DataFrame, htf_interval: str, base_interval: str) -> list[Optional[str]]:
    htf = df_htf.copy()
    htf["bias"] = persistent_bias(combined_state(
        np.array(compute_greenred(htf)), np.array(compute_strategy(htf))))
    htf["close_time"] = htf["time"] + tf_delta(htf_interval)

    base = df_base.copy()
    base["close_time"] = base["time"] + tf_delta(base_interval)

    left = base[["close_time"]].sort_values("close_time")
    right = htf[["close_time", "bias"]].sort_values("close_time").rename(columns={"bias": "htf_bias"})
    merged = pd.merge_asof(left, right, on="close_time", direction="backward")
    return merged["htf_bias"].tolist()

def vol_target_scale(df: pd.DataFrame) -> np.ndarray:
    if not settings.USE_VT:
        return np.ones(len(df))
    # Provide a fallback if diff is empty
    dt_h_series = df["time"].diff().dt.total_seconds()
    dt_h = dt_h_series.median() / 3600.0 if not dt_h_series.isna().all() else 4.0
    
    bpy = (24.0 * 365.0) / dt_h
    win = max(5, int(round(settings.VT_DAYS * bpy / 365.0)))
    ret = df["close"].pct_change()
    vol_annual = ret.rolling(win).std() * np.sqrt(bpy)
    scale = (settings.TARGET_VOL / vol_annual).clip(upper=settings.VT_CAP).shift(1)
    return scale.fillna(0.0).values
