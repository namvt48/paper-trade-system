# 1d-blend-close

| param | value |
|---|---|
| timeframe | 1d (ppy = 365 bars/yr) |
| universe | top-180 by liquidity  (baked into `_t180` fields) |
| rebalance | 1 bars (1d) |
| exec_lag | 1 bar |
| vol-lookback | 30 bars (30d) |
| target_vol | 0.10 | 
| fee | 7 bps per side |
| rule type | cross-sectional rank |

## Formula (DSL)

```
long_when : rank(cs_zscore(ts_mean(returns_t180, 40)) + cs_zscore((close_t180 - ts_min(low_t180, 40)) / (ts_max(high_t180, 40) - ts_min(low_t180, 40)))) > 0.9
short_when: rank(cs_zscore(ts_mean(returns_t180, 40)) + cs_zscore((close_t180 - ts_min(low_t180, 40)) / (ts_max(high_t180, 40) - ts_min(low_t180, 40)))) < 0.1
```

## Signal (score per symbol, then ranked cross-sectionally)

```
score = cs_zscore(ts_mean(returns_t180, 40)) + cs_zscore((close_t180 - ts_min(low_t180, 40)) / (ts_max(high_t180, 40) - ts_min(low_t180, 40)))
```

## Rule

```
long  WHERE cross_sectional_rank(score) > 0.9
short WHERE cross_sectional_rank(score) < 0.1
```

## Logic ra/vào lệnh

- **VÀO LONG:** tại nến rebalance, coin có rank điểm **> 0.9** (nhóm 10% CAO nhất) → mở/giữ long.
- **VÀO SHORT:** coin có rank điểm **< 0.1** (nhóm 10% THẤP nhất) → mở/giữ short.
- **RA:** nến rebalance kế, coin rớt khỏi nhóm → đóng (weight→0); nếu lật sang vế ngược → đảo chiều.
- **Giữ:** vị thế giữ nguyên giữa 2 lần rebalance — mỗi nến (rebalance liên tục). **KHÔNG TP/SL/time-stop.**
- **Khớp:** quyết định ở nến `t`, vào lệnh ở nến `t+1` (exec_lag=1, giá nến kế). Size: equal-weight mỗi vế, dollar-neutral (gross=1), nhân đòn bẩy vol-target.
- Vòng đời đầy đủ: [entry-exit](reference/entry-exit.md).

## Operators used

| op | definition |
|---|---|
| `ts_mean` | `x.rolling(d, min_periods=max(1,d//2)).mean()` |
| `ts_min` | `x.rolling(d, min_periods=1).min()` |
| `ts_max` | `x.rolling(d, min_periods=1).max()` |
| `rank` | `x.rank(axis=1, pct=True)            # cross-sectional percentile [0,1]` |
| `cs_zscore` | `x.sub(x.mean(1),axis=0).div(x.std(1).replace(0,NaN),axis=0)  # per row` |

Fields: `close_t180`, `high_t180`, `low_t180`, `returns_t180` — see [data](reference/data.md).

## Performance (worst-case, 6.6y; IS~5y / OOS=18mo)

| IS Sharpe | OOS Sharpe | maxDD |
|---|---|---|
| 0.94 | 1.93 | -14% |

## Related

- [entry-exit](reference/entry-exit.md) · [operators](reference/operators.md) · [pipeline](reference/pipeline.md) · [data](reference/data.md) · [overview](overview.md)
