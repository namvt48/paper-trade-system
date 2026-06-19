from __future__ import annotations

import numpy as np


def cs_zscore(v: np.ndarray) -> np.ndarray:
    s = np.nanstd(v, ddof=1)
    return (v - np.nanmean(v)) / s if s == s and s > 0 else np.zeros_like(v)


def cs_demean(v: np.ndarray) -> np.ndarray:
    return v - np.nanmean(v)


def cs_winsorize(v: np.ndarray, k: float = 3.0) -> np.ndarray:
    m = np.nanmean(v)
    s = np.nanstd(v, ddof=1)
    return np.clip(v, m - k * s, m + k * s)


def cs_scale(v: np.ndarray, a: float = 1.0) -> np.ndarray:
    s = np.nansum(np.abs(v))
    return v / s * a if s > 0 else np.zeros_like(v)


def cs_rank(v: np.ndarray) -> np.ndarray:
    out = np.full(v.shape, np.nan)
    m = ~np.isnan(v)
    x = v[m]
    if x.size:
        order = x.argsort()
        r = np.empty(x.size)
        r[order] = np.arange(1, x.size + 1)
        out[m] = r / x.size
    return out
