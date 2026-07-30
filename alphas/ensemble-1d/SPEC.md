# ensemble-1d (deploy book)

| param | value |
|---|---|
| timeframe | 1d (ppy = 365 bars/yr) |
| universe | top-180 by liquidity, drawn from a 199-symbol whitelist |
| rebalance | 1 bar (daily) |
| members | `1d-trend60cmf`, `1d-kertrend`, `1d-vwaprev`, `1d-chmom`, `1d-iamp` (all 5, matching the doc) |

## Signal construction

```
members = [trend60cmf, kertrend, vwaprev, chmom, iamp]   # each = own compute_signal_details()
signal  = mean(cs_zscore(member) for member in members)
signal  = ts_ema(signal, ema_smooth=5)                    # denoise
```

## Portfolio overlay (applied to the latest cross-sectional `signal` row)

```
w    = risk_parity(signal, returns, vol_lookback=30)      # inverse-vol tilt, gross=1
w    = beta_neutralize(w, returns, window=60)             # remove market-beta
w    = per_coin_cap(w, cap=0.04)                          # hard clip, NOT gross-preserving
book = drawdown_throttle(w, current_drawdown, floor=-0.08, factor=0.4)
```

## Params

| param | value |
|---|---|
| `overlay.risk_parity.vol_lookback` | 30 |
| `overlay.beta_neutralize.window` | 60 |
| `overlay.per_coin_cap` | 0.04 |
| `overlay.drawdown_throttle.floor` | -0.08 |
| `overlay.drawdown_throttle.factor` | 0.4 |
| `ema_smooth` | 5 |

## Implementation

- `cross_alpha/ensemble.py::combine_members()` — member combination.
- `cross_alpha/overlay.py::{risk_parity,beta_neutralize,per_coin_cap,drawdown_throttle}` — overlay.
- `cross_alpha/strategy.py::select_positions()` — new branch gated on
  `spec.signal == "ensemble_mean"` and `spec.overlay` being set; skips the normal winsor_cont/rank
  construction and long==short trim entirely (this construction is inherently not count-balanced).
- `cross_alpha/engine.py::CrossSectionalEngine` — resolves `spec.members` to loaded `AlphaSpec`s at
  init, tracks `_equity`/`_peak_equity` per rebalance, passes `current_drawdown` into
  `select_positions()`.

## Known differences from `docs/alphas-3/ENSEMBLE-1d.md`

- Not registered in any runner config — build/test only, per this repo's ≥48h-paper-before-live rule.
- `ensemble-1d`'s own Dockerfile doesn't yet copy the 5 member spec.json files in (see README.md
  "Known gap") — member resolution works locally/in tests, not yet inside its own container.
