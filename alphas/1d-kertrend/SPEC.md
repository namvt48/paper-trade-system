# 1d-kertrend (trend-quality)

| param | value |
|---|---|
| timeframe | 1d (ppy = 365 bars/yr) |
| universe | top-180 by liquidity, drawn from a 199-symbol whitelist |
| rebalance | 1 bar (daily) |
| vol-lookback | 30 bars |
| target_vol | 0.10 |
| fee | 7 bps per side |
| rule type | cross-sectional, continuous weight (winsor_cont, k=3.0) |

## Signal (long high)

```
score = ts_ema(kaufman_er(close, er_window), ema_span)
```

Long coin có Kaufman Efficiency Ratio cao = trend "sạch/thẳng" tiếp diễn; trend zigzag mean-revert.
Nhánh đa dạng nhất trong `docs/alphas-3` (corr thấp với các sleeve khác, theo tài liệu gốc).

## Params (see "Assumption flagged" in README.md)

| param | value |
|---|---|
| `field` | `close` |
| `er_window` | 20 (assumed — doc only gives the EMA span) |
| `ema_span` | 20 |

## Operators used

| op | definition |
|---|---|
| `kaufman_er` | `abs(x - x.shift(d)) / rolling_sum(abs(x.diff()), d)` — 1.0 = straight trend, 0.0 = choppy |
| `ts_ema` | `x.ewm(span=d, adjust=False).mean()` |

Implementation: `indicators/pandas/ts_ops.py` (`kaufman_er`, `ts_ema`); wired via
`CrossAlphaComputeContext.kaufman_er`/`.ts_ema` and the `kaufman_trend` signal branch in
`cross_alpha/strategy.py`.
