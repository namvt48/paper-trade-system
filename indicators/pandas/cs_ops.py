from __future__ import annotations

import numpy as np
import pandas as pd


def cs_zscore(x: pd.DataFrame) -> pd.DataFrame:
    return x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1).replace(0, np.nan), axis=0)


def cs_demean(x: pd.DataFrame) -> pd.DataFrame:
    return x.sub(x.mean(axis=1), axis=0)


def cs_winsorize(x: pd.DataFrame, k: float = 3.0) -> pd.DataFrame:
    m = x.mean(axis=1)
    s = x.std(axis=1)
    return x.clip(lower=m - k * s, upper=m + k * s, axis=0)


def cs_scale(x: pd.DataFrame, a: float = 1.0) -> pd.DataFrame:
    s = x.abs().sum(axis=1).replace(0, np.nan)
    return x.div(s, axis=0) * a


def rank(x: pd.DataFrame) -> pd.DataFrame:
    return x.rank(axis=1, pct=True)
