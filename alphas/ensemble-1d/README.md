# ensemble-1d

Generated from `docs/alphas-3/ENSEMBLE-1d.md`, implemented on the `cross_alpha` (ensemble_mean +
portfolio overlay) runtime.

- Combines all 5 members (`1d-trend60cmf`, `1d-kertrend`, `1d-vwaprev`, `1d-chmom`, `1d-iamp`) via
  `mean(cs_zscore(member))`, then `ts_ema(., ema_smooth=5)` denoise (`cross_alpha/ensemble.py`).
  `1d-iamp` was initially deferred (its `ideal_amp` formula wasn't available) and has since been
  added back once the formula was confirmed against `docs/DATA_DICTIONARY.md` — see
  `alphas/1d-iamp/README.md`.
- Portfolio overlay (`cross_alpha/overlay.py`), applied in order: `risk_parity` (inverse 30d
  realized-vol tilt) → `beta_neutralize` (60d equal-weight-market beta removed) → `per_coin_cap`
  (4%, hard clip — **not** gross-preserving, exposure is expected to drop when it binds) →
  `drawdown_throttle` (cuts exposure to 0.4x once the book's drawdown from peak equity breaches -8%).
- Peak-equity / current-drawdown tracking lives in `cross_alpha/engine.py`'s `CrossSectionalEngine`
  (`_equity`, `_peak_equity`, `_current_drawdown()`), driving the throttle step above.
- Rebalances every daily bar; executes at candle close (no exec lag).

## Known gap (deployment, not build/test)

`CrossSectionalEngine.__init__` resolves each `members` entry to
`<alphas_root>/<member_id>/spec.json`, where `<alphas_root>` is `SPEC_FILE`'s grandparent directory.
That resolves correctly when running locally against this repo's `alphas/` layout (verified by
tests), but **not yet inside this alpha's own Docker container** — the `Dockerfile` doesn't copy the
5 member directories' `spec.json` files in, since this alpha isn't registered/deployed (per this
repo's ≥48h-paper-before-live rule, build/test only for now). Needs a Dockerfile update (or an
explicit `alphas_root` override) before containerized deployment.

Every closed strategy-timeframe candle writes one `SIGNAL_AUDIT` JSON line to `logs/bot.log`.
