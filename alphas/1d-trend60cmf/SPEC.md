# 1d-trend60cmf (quiet-momentum)

| param | value |
|---|---|
| timeframe | 1d (ppy = 365 bars/yr) |
| universe | own 137-symbol liquidity-filtered whitelist (matches doc's "~137 coin"), not the shared 199-symbol one |
| rebalance | 1 bar (daily) |
| vol-lookback | 30 bars |
| target_vol | 0.10 |
| fee | 7 bps per side |
| rule type | cross-sectional, continuous weight (winsor_cont, k=3.0) |

## Signal (long high)

```
score = cs_zscore(ts_zscore(close, 60)) - cs_zscore(ts_ema(cmf(high, low, close, volume, cmf_window), ema_span))
```

Long coin có trend-giá 60d cao NHƯNG money-flow (CMF) chưa xác nhận = momentum trước khi dòng
tiền crowd vào. `cmf` = Chaikin Money Flow (derived, causal).

## Params

| param | value |
|---|---|
| `z_window` | 60 |
| `cmf_window` | 20 (confirmed against `docs/DATA_DICTIONARY.md` §3 "Lượng-giá" — cmf's own standard window) |
| `ema_span` | 20 |

## Operators used

| op | definition |
|---|---|
| `ts_zscore` | `(x - ts_mean(x,d)) / ts_std(x,d).replace(0, NaN)` |
| `cmf` | `rolling_sum(((c-l)-(h-c))/(h-l) * volume, d) / rolling_sum(volume, d)` |
| `ts_ema` | `x.ewm(span=d, adjust=False).mean()` |
| `cs_zscore` | `(x - x.mean(axis=1)) / x.std(axis=1)` — cross-sectional |

Implementation: `indicators/pandas/ts_ops.py` (`cmf`, `ts_ema`); wired via
`CrossAlphaComputeContext.cmf`/`.ts_ema` and the `trend_cmf_blend` signal branch in
`cross_alpha/strategy.py`.
