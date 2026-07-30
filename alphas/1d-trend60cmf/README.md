# 1d-trend60cmf

Generated from `docs/alphas-3/1d-trend60cmf.md`, implemented on the `cross_alpha` (winsor_cont)
runtime — same pattern as `1h-decay-close`.

- Cross-sectional portfolio; no TP, SL, or time stop.
- Rebalances every daily bar; executes at candle close (no exec lag).
- Continuous-weight construction (`winsor_cont`): weight ∝ clipped cs_zscore of the score.
- **Own 137-symbol whitelist** (not the shared 199-symbol one the other new alphas use) — matches
  the doc's own universe note ("TRADABLE, liquidity-filtered, ~137 coin"). Provided directly (all
  137 confirmed as a subset of the shared 199-symbol whitelist, no typos). `spec.json`'s
  `universe_size: 180` is left unchanged (a `dynamic_top_k` cap above the whitelist's own 137 is a
  harmless no-op — every whitelisted symbol passes — rather than a functional 180-symbol universe).
- `cmf_window=20`: the doc's formula only shows the EMA smoothing span (20); confirmed separately
  against `docs/DATA_DICTIONARY.md` that cmf's own standard window is also 20d — not a guess.
- Doc calls this the "best single [alpha] on tradable" — highest OS Sharpe (1.41) of the 5
  standalone members feeding ENSEMBLE-1d.
- Every closed strategy-timeframe candle writes one `SIGNAL_AUDIT` JSON line to `logs/bot.log`.
- Not registered in any runner config — build/test only, per this repo's ≥48h-paper-before-live rule.
