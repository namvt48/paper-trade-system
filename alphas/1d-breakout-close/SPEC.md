# 1d-breakout-close

| param | value |
|---|---|
| timeframe | 1d (ppy = 365 bars/yr) |
| universe | top-180 by liquidity  (baked into `_t180` fields) |
| rebalance | 1 bars (1d) |
| exec_lag | 1 bar |
| vol-lookback | 30 bars (30d) |
| target_vol | 0.10 | 
| fee | 7 bps per side |
| rule type | absolute threshold |

## Formula (DSL)

```
long_when : ts_zscore(high_t180, 60) > 1.5
short_when: ts_zscore(volume_t180, 10) < -1.5
```

## Rule

```
long  WHERE ts_zscore(high_t180, 60) > 1.5
short WHERE ts_zscore(volume_t180, 10) < -1.5
```

## Logic ra/vào lệnh

- **VÀO LONG:** coin thỏa `ts_zscore(high_t180, 60) > 1.5`.
- **VÀO SHORT:** coin thỏa `ts_zscore(volume_t180, 10) < -1.5`.
- **RA:** khi điều kiện không còn đúng ở nến rebalance kế → đóng.
- **Giữ:** vị thế giữ nguyên giữa 2 lần rebalance — mỗi nến (rebalance liên tục). **KHÔNG TP/SL/time-stop.**
- **Khớp:** quyết định ở nến `t`, vào lệnh ở nến `t+1` (exec_lag=1, giá nến kế). Size: equal-weight mỗi vế, dollar-neutral (gross=1), nhân đòn bẩy vol-target.
- Vòng đời đầy đủ: [entry-exit](reference/entry-exit.md).

## Operators used

| op | definition |
|---|---|
| `ts_zscore` | `(x - ts_mean(x,d)) / ts_std(x,d).replace(0, NaN)` |

Fields: `high_t180`, `volume_t180` — see [data](reference/data.md).

## Performance (worst-case, 6.6y; IS~5y / OOS=18mo)

| IS Sharpe | OOS Sharpe | maxDD |
|---|---|---|
| 1.48 | 1.64 | -14% |

## Related

- [entry-exit](reference/entry-exit.md) · [operators](reference/operators.md) · [pipeline](reference/pipeline.md) · [data](reference/data.md) · [overview](overview.md)
