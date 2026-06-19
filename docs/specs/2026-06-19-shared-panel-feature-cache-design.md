# Shared Panel & Feature Cache Design

## Problem

18 cross-sectional alphas run in one runner process. Each alpha independently calls `build_panel(snapshot)` + `compute_signal_details()` on the same data. When 5 alphas on the same TF receive the same kline event:

- `build_panel()` runs 5 times producing 5 identical DataFrames
- Rolling primitives (`_ts_zscore`, `_ts_skew`, etc.) recompute for the same `(field, window)` pairs
- Example: `zscore(close, 5760)` computed twice (15m-blend-close, 15m-blend-close-c)

## Architecture

Three cache layers, each keyed differently:

```
SharedCandleCache ──► SharedPanelCache ──► FeatureCache
(raw candles)        (DataFrame/panel)    (rolling features)
```

### Layer 1: SharedCandleCache (existing, unchanged)

Key: `(symbol, tf)` → raw candle data. Already exists, no changes.

### Layer 2: SharedPanelCache (new)

- Key: `timeframe` → `{field: DataFrame}` for all symbols
- `build_panel()` called once per TF per candle
- Fields: `close`, `high`, `low`, `volume`, `vwap`, `quote_volume`, `returns`
- Invalidate when new candle arrives for that TF
- Lazy: only built when an alpha requests it

### Layer 3: FeatureCache (new)

- Key: `(tf, field, transform, window)` → DataFrame
- Examples:
  - `("15m", "close", "zscore", 5760)` → zscore(close, 5760) for all symbols
  - `("1h", "close", "decay", (2880, 240))` → decay(zscore(close, 2880), 240)
- Invalidate when panel cache for that TF invalidates
- Each alpha declares feature dependencies from its spec, retrieves cached features

## Cache Invalidation

When kline event arrives for TF X:

1. `SharedCandleCache.upsert_candle()` (existing)
2. `SharedPanelCache.invalidate(X)` — mark dirty, rebuild on next access
3. `FeatureCache.invalidate(X)` — drop all features for TF X

Lazy evaluation: panels and features only built when an alpha needs them, no eager building.

## Feature Key Derivation from Spec

Each signal type declares feature dependencies deterministically:

| Signal | Feature Keys |
|--------|-------------|
| `zscore` | `(tf, field, "zscore", window)` |
| `momentum` | `(tf, field, "momentum", window)` |
| `decay_zscore` | `(tf, field, "zscore", z_window)`, `(tf, field, "decay", (z_window, decay))` |
| `blend_zscore_skew` | `(tf, "close", "zscore", z_window)`, `(tf, "returns", "skew", skew_window)` |
| `blend_momvol_skew` | `(tf, "close", "momentum", momentum_window)`, `(tf, "returns", "std", std_window)`, `(tf, "returns", "skew", skew_window)` |
| `blend_zscore_meanret` | `(tf, "close", "zscore", z_window)`, `(tf, "returns", "mean", mean_window)` |
| `blend_zscore_momvol` | `(tf, "close", "zscore", z_window)`, `(tf, "close", "momentum", momentum_window)`, `(tf, "returns", "std", std_window)` |
| `blend_zscore_decayz` | `(tf, "close", "zscore", first_z_window)`, `(tf, "close", "zscore", second_z_window)`, `(tf, "close", "decay", (second_z_window, decay))` |
| `blend_decayz_meanret` | `(tf, "close", "zscore", z_window)`, `(tf, "close", "decay", (z_window, decay))`, `(tf, "returns", "mean", mean_window)` |
| `blend_meanret_range` | `(tf, "returns", "mean", mean_window)`, `(tf, "close", "range_location", range_window)` |
| `blend_zscore_volume_zscore` | `(tf, "close", "zscore", close_window)`, `(tf, "volume", "zscore", volume_window)` |
| `blend_decayz_volume_zscore` | `(tf, "close", "zscore", close_window)`, `(tf, "close", "decay", (close_window, decay))`, `(tf, "volume", "zscore", volume_window)` |
| `absolute_breakout` | `(tf, long_field, "zscore", long_window)`, `(tf, short_field, "zscore", short_window)` |

## Integration with CrossSectionalRunnerStrategy

Before (each alpha builds independently):
```python
panel = build_panel(snapshot)
selection = select_positions(panel, self.spec)
```

After (shared cache):
```python
panel = self.ctx.panel_cache.get_panel(self.spec.timeframe, self.ctx.cache)
features = self.ctx.feature_cache.compute_features(panel, self.spec)
selection = select_positions_from_features(features, self.spec)
```

`select_positions_from_features` replaces `compute_signal_details` — uses cached features instead of recomputing.

The existing `select_positions` and `compute_signal_details` remain unchanged for backward compatibility (standalone alphas outside the runner).

## Files Changed

| File | Change |
|------|--------|
| `cross_alpha/panel_cache.py` (new) | `SharedPanelCache` class |
| `cross_alpha/feature_cache.py` (new) | `FeatureCache` class + key derivation |
| `cross_alpha/strategy.py` | Add `select_positions_from_features()`, keep backward compat |
| `runner/strategy/context.py` | Add `panel_cache`, `feature_cache` fields |
| `runner/main.py` | Init `SharedPanelCache`, `FeatureCache`, pass to context |

## RAM Estimate

- Panel: 5 TF × 7 fields × ~528 symbols × ~3120 bars × 8B ≈ ~450 MB (but only 1-2 TFs active concurrently)
- Features: ~25 unique `(tf, field, transform, window)` combinations × 528 symbols × ~3120 bars × 8B ≈ ~300 MB
- **Total additional**: ~300 MB for features (panel data already in SharedCandleCache as raw candles, just format conversion)
- Eviction: trim features older than warmup baseline, matching SharedCandleCache behavior

## Performance Expectation

- `build_panel()`: 5x → 1x per TF per candle = **~5x speedup** for group of 5 alphas on same TF
- Rolling features: 2-3x dedup (same field+window computed once) = **~2-3x additional speedup**
- Combined: **~5-10x fewer DataFrame operations** per candle event

## Concrete Example: 15m Group

5 alphas on 15m receive the same candle event:

Without cache: 5 × build_panel + 5 × rolling (with overlaps)
With cache: 1 × build_panel + 6 unique rolling features (zscore_close_5760, skew_returns_3840, zscore_close_8640, zscore_vwap_8640, mean_returns_1920, momentum_close_1920 / std_returns_1920)

Shared features:
- `zscore(close, 5760)`: used by 15m-blend-close and 15m-blend-close-c
- `skew(returns, 3840)`: used by 15m-blend-close and 15m-blend-close-b
