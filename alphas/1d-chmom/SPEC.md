# 1d-chmom (carry-adjusted momentum)

| param | value |
|---|---|
| timeframe | 1d (ppy = 365 bars/yr) |
| universe | top-180 by liquidity, drawn from a 199-symbol whitelist |
| rebalance | 1 bar (daily) |
| vol-lookback | 30 bars |
| target_vol | 0.10 |
| fee | 7 bps per side |
| rule type | cross-sectional, continuous weight (winsor_cont, k=3.0) |
| needs_funding | true |

## Signal (long high)

```
funding_zscore = ts_zscore(funding, funding_window)   # at funding's OWN settlement frequency (~8h)
score = cs_zscore(ts_momentum(close, momentum_window)) - cs_zscore(ts_ema(funding_zscore, ema_span))
```

Momentum RẺ để giữ: long coin đang lên mà funding-zscore chưa cao (chưa crowded phái sinh).

**Confirmed against `~/Desktop/datacryp/_scripts/_build_derived_v2.py::build_funding()`**:
`funding_zscore21` = `(f - rolling_mean(f,21)) / rolling_std(f,21)` where `f` is the raw funding rate
at its own **settlement frequency** (~8h/row) — 21 settlements ≈ 7 days, NOT 21 daily bars. The
z-score is computed at that native frequency, then reindexed/ffilled onto this alpha's daily kline
index — see `CrossSectionalRunnerStrategy._attach_funding_panel()`. `carry_momentum`'s branch in
`cross_alpha/strategy.py` consumes the already-zscored `fields["funding_zscore"]` directly (does
**not** call `ts_zscore` again — that would double-apply the window, and at the wrong frequency).

## Params

| param | value |
|---|---|
| `momentum_window` | 20 (daily bars) |
| `funding_window` | 21 (funding **settlements**, ~8h each ≈ 7d — not daily bars) |
| `ema_span` | 3 (daily bars, applied after reindex) |

## Operators used

| op | definition |
|---|---|
| `ts_momentum` | `x / x.shift(d) - 1` |
| `ts_zscore` | `(x - ts_mean(x,d)) / ts_std(x,d).replace(0, NaN)` |
| `ts_ema` | `x.ewm(span=d, adjust=False).mean()` |
| `cs_zscore` | `(x - x.mean(axis=1)) / x.std(axis=1)` — cross-sectional |

Implementation: `carry_momentum` signal branch in `cross_alpha/strategy.py`. Funding data path:
`runner/data_layer/funding_snapshot.py` (`FundingSnapshotReader`) →
`cross_alpha/strategy.py`'s `build_funding_panel()` →
`CrossSectionalRunnerStrategy._attach_funding_panel()` (reindexed/ffilled onto the daily kline index).
