# 1d-vwaprev

Generated from `docs/alphas-3/1d-vwaprev.md`, implemented on the `cross_alpha` (winsor_cont)
runtime — same pattern as `1h-decay-close`.

- Cross-sectional portfolio; no TP, SL, or time stop.
- Rebalances every daily bar; executes at candle close (no exec lag).
- Continuous-weight construction (`winsor_cont`): weight ∝ clipped cs_zscore of the score.
- **VWAP(20d) is computed on 1d candles** (rolling dollar-volume-weighted average price over
  the last 20 daily bars), not a 4h cross-timeframe build — the doc's "Cross-TF (1d+4h)" note
  is treated as a possible future refinement, not required for v1 (confirmed with user).
- Every closed strategy-timeframe candle writes one `SIGNAL_AUDIT` JSON line to `logs/bot.log`.
- Not registered in any runner config — build/test only, per this repo's ≥48h-paper-before-live rule.
