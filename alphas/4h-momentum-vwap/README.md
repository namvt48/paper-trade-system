# 4h-momentum-vwap

Generated from `../alpha/alpha/4h-momentum-vwap.md` using the `alpha-1-v5b` runtime layout.

- Cross-sectional portfolio; no TP, SL, or time stop.
- Rebalances after the configured hold interval and executes one bar later.
- Uses separate equal-weight long/short legs for true dollar neutrality.
- Current MDS does not expose quote volume, so VWAP and dollar-volume fields use
  explicitly reported proxies until MDS adds `quote_volume`.
- Current Binance MDS warmup returns at most 1500 bars. Specs requiring more
  bars remain inactive until the MDS warmup endpoint is made paginated.
- The paper worker cannot modify an open position quantity. To preserve target
  weights, the runtime closes and reopens the basket at each rebalance.
- Every closed strategy-timeframe candle writes one `SIGNAL_AUDIT` JSON line to
  `logs/bot.log`, including every symbol's formula components, score, rank,
  decision, and target weight.
