from __future__ import annotations

import numpy as np
import pandas as pd

from indicators.pandas.ts_ops import ts_std


def risk_parity(signal: pd.Series, returns: pd.DataFrame, vol_lookback: int) -> pd.Series:
    """Inverse-vol tilt: weight_i ∝ signal_i / realized_vol_i(vol_lookback),
    then rescaled to gross exposure 1. Symbols with a zero (or undefined,
    e.g. too little history) realized vol are dropped rather than divided
    by zero -- a genuinely flat symbol contributes no diversification and
    would otherwise blow up to an infinite tilt."""
    common = signal.index.intersection(returns.columns)
    vol = ts_std(returns[common], int(vol_lookback)).iloc[-1]
    tilted = (signal[common] / vol.replace(0, np.nan)).dropna()
    gross = tilted.abs().sum()
    if gross <= 0:
        return tilted * 0.0
    return tilted / gross


def beta_neutralize(weights: pd.Series, returns: pd.DataFrame, window: int) -> pd.Series:
    """Remove the book's net market-beta exposure via an OLS-style projection:
    subtract beta * (portfolio_beta / sum(beta^2)) from each weight, so the
    resulting portfolio beta (sum(w_i * beta_i)) is ~0. Market proxy is the
    equal-weight average return of the symbols actually held."""
    symbols = [s for s in weights.index if s in returns.columns]
    if not symbols:
        return weights
    tail_returns = returns[symbols].tail(int(window))
    market = tail_returns.mean(axis=1)
    market_var = market.var()
    if not market_var or pd.isna(market_var):
        return weights
    beta = tail_returns.apply(lambda col: col.cov(market) / market_var).reindex(weights.index).fillna(0.0)
    portfolio_beta = float((weights * beta).sum())
    beta_sq_sum = float((beta ** 2).sum())
    if beta_sq_sum == 0:
        return weights
    adjustment = beta * (portfolio_beta / beta_sq_sum)
    return weights - adjustment


def per_coin_cap(weights: pd.Series, cap: float) -> pd.Series:
    """Hard cap on |weight| per symbol -- a concentration/risk control, not a
    gross-exposure-preserving rescale. Gross exposure is allowed to drop
    when the cap binds (matches the doc's overlay, which shows avg_gross
    well under 1.0 after cap + drawdown-throttle)."""
    cap = abs(float(cap))
    return weights.clip(lower=-cap, upper=cap)


def drawdown_throttle(weights: pd.Series, current_drawdown: float, floor: float, factor: float) -> pd.Series:
    """Scale all weights by ``factor`` once the book's drawdown from its peak
    (a negative number, e.g. -0.10 for -10%) breaches ``floor``; otherwise
    pass the weights through unchanged."""
    if current_drawdown < floor:
        return weights * float(factor)
    return weights
