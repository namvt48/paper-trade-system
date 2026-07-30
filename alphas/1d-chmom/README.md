# 1d-chmom

Generated from `docs/alphas-3/1d-chmom.md`, implemented on the `cross_alpha` (winsor_cont)
runtime — same pattern as `1h-decay-close`.

- Cross-sectional portfolio; no TP, SL, or time stop.
- Rebalances every daily bar; executes at candle close (no exec lag).
- Continuous-weight construction (`winsor_cont`): weight ∝ clipped cs_zscore of the score.
- **Only alpha in `docs/alphas-3` that needs funding-rate data.** `spec.json` sets
  `needs_funding: true`, which makes the runner (`CrossSectionalRunnerStrategy._attach_funding_panel`)
  fetch a cross-sectional funding panel from MDS's `funding_snapshot:{exchange}:{symbol}` Redis keys
  and merge it into the panel before scoring. MDS's `FUNDING_SYMBOLS` must cover this alpha's
  whitelist (confirmed already expanded to the full 199-symbol list, see session notes).
- Every closed strategy-timeframe candle writes one `SIGNAL_AUDIT` JSON line to `logs/bot.log`.
- Not registered in any runner config — build/test only, per this repo's ≥48h-paper-before-live rule.
