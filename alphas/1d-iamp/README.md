# 1d-iamp

Generated from `docs/alphas-3/1d-iamp.md`, implemented on the `cross_alpha` (winsor_cont) runtime —
same pattern as `1h-decay-close`. Previously deferred in this plan (no formula was available); the
formula was confirmed against `docs/DATA_DICTIONARY.md` +
`~/Desktop/datacryp/_scripts/_build_derived_v4.py::build_amplitude()` and implemented as
`indicators/pandas/ts_ops.py::ideal_amp()`.

- Cross-sectional portfolio; no TP, SL, or time stop.
- Rebalances every daily bar; executes at candle close (no exec lag).
- Continuous-weight construction (`winsor_cont`): weight ∝ clipped cs_zscore of the score.
- `ideal_amp` (理想振幅): within a trailing 20-VALID-bar window, split days into the top-5/bottom-5
  by CLOSE level (count-based, not a quantile threshold), then
  `mean(amplitude on high-close days) - mean(amplitude on low-close days)`, where
  `amplitude = high/low - 1` clipped at 300%. NaN bars are skipped entirely (not counted toward the
  window) — a symbol needs ≥25 valid bars (window+5) before it produces any output.
- Every closed strategy-timeframe candle writes one `SIGNAL_AUDIT` JSON line to `logs/bot.log`.
- Not registered in any runner config — build/test only, per this repo's ≥48h-paper-before-live rule.
