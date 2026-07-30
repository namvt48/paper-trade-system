# 1d-iamp (lottery-amplitude)

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
amp = clip(high/low - 1, upper=3.0)
ideal_amp = mean(amp | close in top-k of trailing window) - mean(amp | close in bottom-k)
            # window=20 VALID bars, k=int(0.25*window)=5, count-based (not quantile threshold)
score = ts_ema(ideal_amp, ema_span=1)   # ema_span=1 -> passthrough, no extra smoothing
```

Long coin có 理想振幅 cao (biên độ dồn vào ngày giá cao) — crypto lật dấu vs A-share. Diversifier.

**Formula confirmed** against `docs/DATA_DICTIONARY.md` §3 "理想振幅" and
`~/Desktop/datacryp/_scripts/_build_derived_v4.py::build_amplitude()`/`_rolling_split_diff()` — this
was the one alpha in `docs/alphas-3` initially deferred for lack of a formula.

## Params

| param | value |
|---|---|
| `window` | 20 |
| `k_frac` | 0.25 (k = 5 days, by count — not a quantile threshold) |
| `ema_span` | 1 |

## Operators used

| op | definition |
|---|---|
| `ideal_amp` | see `indicators/pandas/ts_ops.py::ideal_amp()` — top-k/bottom-k split-diff, NaN-skipping |
| `ts_ema` | `x.ewm(span=d, adjust=False).mean()` |

Implementation: `indicators/pandas/ts_ops.py` (`ideal_amp`); wired via
`CrossAlphaComputeContext.ideal_amp`/`.ts_ema` and the `ideal_amplitude` signal branch in
`cross_alpha/strategy.py`.
