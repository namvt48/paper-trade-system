# 1d-blend-close-c

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
long_when : rank(cs_zscore(decay_linear(ts_zscore(close_t180, 60), 5)) + cs_zscore(ts_zscore(volume_t180, 20))) > 0.9
short_when: rank(cs_zscore(decay_linear(ts_zscore(close_t180, 60), 5)) + cs_zscore(ts_zscore(volume_t180, 20))) < 0.1
```

## Signal (score per symbol, then ranked cross-sectionally)

```
score = cs_zscore(decay_linear(ts_zscore(close_t180, 60), 5)) + cs_zscore(ts_zscore(volume_t180, 20))
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
| `ts_zscore` | `(x - ts_mean(x,d)) / ts_std(x,d).replace(0, NaN)` |
| `decay_linear` | `w = arange(d,0,-1); w/=w.sum(); sum_{k=0..d-1} w[k]*x.shift(k)` |
| `rank` | `x.rank(axis=1, pct=True)            # cross-sectional percentile [0,1]` |
| `cs_zscore` | `x.sub(x.mean(1),axis=0).div(x.std(1).replace(0,NaN),axis=0)  # per row` |

Fields: `close_t180`, `volume_t180` — see [data](reference/data.md).

## Performance (worst-case, 6.6y; IS~5y / OOS=18mo)

| IS Sharpe | OOS Sharpe | maxDD |
|---|---|---|
| 0.95 | 1.53 | -11% |

## Related

- [entry-exit](reference/entry-exit.md) · [operators](reference/operators.md) · [pipeline](reference/pipeline.md) · [data](reference/data.md) · [overview](overview.md)
