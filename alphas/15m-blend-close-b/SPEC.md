# 15m-blend-close-b

| param | value |
|---|---|
| timeframe | 15m (ppy = 35040 bars/yr) |
| universe | top-180 by liquidity  (baked into `_t180` fields) |
| rebalance | 192 bars (2d) |
| exec_lag | 1 bar |
| vol-lookback | 480 bars (5d) |
| target_vol | 0.10 | 
| fee | 7 bps per side |
| rule type | cross-sectional rank |

## Formula (DSL)

```
long_when : rank(cs_zscore(ts_momentum(close_t180, 1920) / ts_std(returns_t180, 1920)) + cs_zscore(-ts_skew(returns_t180, 3840))) > 0.9
short_when: rank(cs_zscore(ts_momentum(close_t180, 1920) / ts_std(returns_t180, 1920)) + cs_zscore(-ts_skew(returns_t180, 3840))) < 0.1
```

## Signal (score per symbol, then ranked cross-sectionally)

```
score = cs_zscore(ts_momentum(close_t180, 1920) / ts_std(returns_t180, 1920)) + cs_zscore(-ts_skew(returns_t180, 3840))
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
- **Giữ:** vị thế giữ nguyên giữa 2 lần rebalance — 192 nến (2d). **KHÔNG TP/SL/time-stop.**
- **Khớp:** quyết định ở nến `t`, vào lệnh ở nến `t+1` (exec_lag=1, giá nến kế). Size: equal-weight mỗi vế, dollar-neutral (gross=1), nhân đòn bẩy vol-target.
- Vòng đời đầy đủ: [entry-exit](reference/entry-exit.md).

## Operators used

| op | definition |
|---|---|
| `ts_std` | `x.rolling(d, min_periods=max(2,d//2)).std()   # sample std, ddof=1` |
| `ts_momentum` | `x / x.shift(d) - 1.0` |
| `ts_skew` | `x.rolling(d, min_periods=max(3,d//2)).skew()` |
| `rank` | `x.rank(axis=1, pct=True)            # cross-sectional percentile [0,1]` |
| `cs_zscore` | `x.sub(x.mean(1),axis=0).div(x.std(1).replace(0,NaN),axis=0)  # per row` |

Fields: `close_t180`, `returns_t180` — see [data](reference/data.md).

## Performance (worst-case, 6.6y; IS~5y / OOS=18mo)

| IS Sharpe | OOS Sharpe | maxDD |
|---|---|---|
| 1.55 | 3.22 | -12% |

## Related

- [entry-exit](reference/entry-exit.md) · [operators](reference/operators.md) · [pipeline](reference/pipeline.md) · [data](reference/data.md) · [overview](overview.md)
