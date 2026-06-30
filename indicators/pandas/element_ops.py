from __future__ import annotations

import pandas as pd


def abs(x: pd.DataFrame) -> pd.DataFrame:
    return x.abs()


def neg(x: pd.DataFrame) -> pd.DataFrame:
    return -x


def add(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    return left + right


def div(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    return left / right
