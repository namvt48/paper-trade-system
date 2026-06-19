from __future__ import annotations

import pandas as pd
from indicators.streaming.cross_sectional import (
    cs_zscore as _cs_zscore,
    cs_demean as _cs_demean,
    cs_winsorize as _cs_winsorize,
    cs_scale as _cs_scale,
    cs_rank as _cs_rank,
)


def cs_zscore(x: pd.Series) -> pd.Series:
    return pd.Series(_cs_zscore(x.to_numpy()), index=x.index)


def cs_demean(x: pd.Series) -> pd.Series:
    return pd.Series(_cs_demean(x.to_numpy()), index=x.index)


def cs_winsorize(x: pd.Series, k: float = 3.0) -> pd.Series:
    return pd.Series(_cs_winsorize(x.to_numpy(), k), index=x.index)


def cs_scale(x: pd.Series, a: float = 1.0) -> pd.Series:
    return pd.Series(_cs_scale(x.to_numpy(), a), index=x.index)


def rank(x: pd.Series) -> pd.Series:
    return pd.Series(_cs_rank(x.to_numpy()), index=x.index)
