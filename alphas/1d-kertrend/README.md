# 1d-kertrend

Generated from `docs/alphas-3/1d-kertrend.md`, implemented on the `cross_alpha` (winsor_cont)
runtime — same pattern as `1h-decay-close`.

- Cross-sectional portfolio; no TP, SL, or time stop.
- Rebalances every daily bar; executes at candle close (no exec lag).
- Continuous-weight construction (`winsor_cont`): weight ∝ clipped cs_zscore of the score,
  not a fixed top/bottom-10% bucket.
- **Assumption flagged for review**: the doc's formula (`score = ts_ema(kaufman_er, 20)`) only
  states the EMA smoothing span (20); Kaufman Efficiency Ratio's own lookback window isn't
  given. Defaulted to `er_window=20` (same as the smoothing span) — confirm/tune against the
  original backtest before live registration.
- Every closed strategy-timeframe candle writes one `SIGNAL_AUDIT` JSON line to `logs/bot.log`.
- Not registered in any runner config — build/test only, per this repo's ≥48h-paper-before-live rule.
