# 1d-vwaprev (VWAP-stretch continuation)

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
vwap_dev = close / ts_vwap(high, low, close, volume, vwap_window) - 1
score    = ts_ema(vwap_dev, ema_span)
```

Long coin giá kéo dãn TRÊN VWAP = breakout ngắn hạn tiếp diễn.

## Params

| param | value |
|---|---|
| `vwap_window` | 20 (days) |
| `ema_span` | 1 (i.e. no smoothing beyond the raw deviation — `ts_ema(x,1)` is a passthrough) |

## Operators used

| op | definition |
|---|---|
| `ts_vwap` | `rolling_sum(typical_price * volume, d) / rolling_sum(volume, d)`, typical=(h+l+c)/3 |
| `ts_ema` | `x.ewm(span=d, adjust=False).mean()` |

Implementation: `indicators/pandas/ts_ops.py` (`ts_vwap`, `ts_ema`); wired via
`CrossAlphaComputeContext.ts_vwap`/`.ts_ema` and the `vwap_reversion` signal branch in
`cross_alpha/strategy.py`. Note this real, volume-weighted VWAP is separate from `build_panel()`'s
existing `panel["vwap"]` field, which remains a typical-price proxy used by no signal today.
