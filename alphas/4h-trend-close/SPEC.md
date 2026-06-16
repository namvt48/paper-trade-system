# 4h-trend-close

| param | value |
|---|---|
| timeframe | 4h (ppy = 2190 bars/yr) |
| universe | top-60 by liquidity  (liquid-K filter at backtest) |
| rebalance | 12 bars (2d) |
| exec_lag | 1 bar |
| vol-lookback | 30 bars (5d) |
| target_vol | 0.10 | 
| fee | 7 bps per side |
| rule type | cross-sectional rank |

## Formula (DSL)

```
long_when : rank(ts_zscore(close, 540)) > 0.93
short_when: rank(ts_zscore(close, 540)) < 0.07
```

## Signal (score per symbol, then ranked cross-sectionally)

```
score = ts_zscore(close, 540)
```

## Rule

```
long  WHERE cross_sectional_rank(score) > 0.93
short WHERE cross_sectional_rank(score) < 0.07
```

## Logic ra/vào lệnh

- **VÀO LONG:** tại nến rebalance, coin có rank điểm **> 0.93** (nhóm 7% CAO nhất) → mở/giữ long.
- **VÀO SHORT:** coin có rank điểm **< 0.07** (nhóm 7% THẤP nhất) → mở/giữ short.
- **RA:** nến rebalance kế, coin rớt khỏi nhóm → đóng (weight→0); nếu lật sang vế ngược → đảo chiều.
- **Giữ:** vị thế giữ nguyên giữa 2 lần rebalance — 12 nến (2d). **KHÔNG TP/SL/time-stop.**
- **Khớp:** quyết định ở nến `t`, vào lệnh ở nến `t+1` (exec_lag=1, giá nến kế). Size: equal-weight mỗi vế, dollar-neutral (gross=1), nhân đòn bẩy vol-target.
- Vòng đời đầy đủ: [entry-exit](reference/entry-exit.md).

## Operators used

| op | definition |
|---|---|
| `ts_zscore` | `(x - ts_mean(x,d)) / ts_std(x,d).replace(0, NaN)` |
| `rank` | `x.rank(axis=1, pct=True)            # cross-sectional percentile [0,1]` |

Fields: `close` — see [data](reference/data.md).

## Performance (worst-case, 6.6y; IS~5y / OOS=18mo)

| IS Sharpe | OOS Sharpe | maxDD |
|---|---|---|
| 1.39 | 1.12 | -13% |

## Related

- [entry-exit](reference/entry-exit.md) · [operators](reference/operators.md) · [pipeline](reference/pipeline.md) · [data](reference/data.md) · [overview](overview.md)
