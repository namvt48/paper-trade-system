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
- Built from `SharedCandleCache.snapshot()` data (same logic as current `build_panel`, but reading from cache instead of per-alpha snapshot)
- Fields: `close`, `high`, `low`, `volume`, `vwap` (typical price proxy), `quote_volume` (close × volume), `returns` (close.pct_change)
- `returns` is a derived field computed once during panel build, not a separate rolling feature
- Invalidate when new candle arrives for that TF
- Lazy: only built when an alpha requests it

### Layer 3: FeatureCache (new)

- Key: `(tf, field, transform, window, universe_mode, universe_size)` → DataFrame
- `universe_mode` and `universe_size` are part of the key because `_masked_fields` (dynamic_top_k) replaces non-qualifying symbol values with NaN **before** rolling computation, producing different results than unmasked data
- For `current_top_k` alphas, universe_mode in key is `"none"` (no pre-mask applied; filtering happens post-score)
- Examples:
  - `("15m", "close", "zscore", 5760, "dynamic_top_k", 180)` → zscore(masked_close, 5760)
  - `("1h", "close", "decay", (2880, 240), "dynamic_top_k", 180)` → decay(zscore(masked_close, 2880), 240)
  - `("4h", "close", "zscore", 540, "none", 0)` → zscore(raw_close, 540) for current_top_k alphas
- Invalidate when panel cache for that TF invalidates
- Each alpha declares feature dependencies from its spec, retrieves cached features
- Multi-step transforms resolve dependencies internally: requesting `decay(zscore(close, 2880), 240)` auto-computes and caches the intermediate `zscore(close, 2880)` if not already cached

## Masking and Feature Key Design

`_masked_fields` applies a liquidity mask that sets non-qualifying symbols to NaN. This changes rolling computation results because:

- `zscore(masked_close, 5760)`: NaN symbols excluded from rolling mean/std
- `zscore(raw_close, 5760)`: all symbols included

Therefore the feature key must include `(universe_mode, universe_size)`:

| Universe Config | Effect on Features | Key Component |
|----------------|-------------------|---------------|
| `dynamic_top_k, 180` | Pre-mask fields → NaN for symbols outside top 180 | `("dynamic_top_k", 180)` |
| `current_top_k, 60` | No pre-mask (filtering post-score only) | `("none", 0)` |
| `current_top_k, 180` | No pre-mask (same as above) | `("none", 0)` |

In practice: 15 of 18 alphas use `dynamic_top_k, 180` → same mask → same feature results. 3 alphas use `current_top_k` → no mask → separate feature cache branch. Within each branch, features with identical `(field, transform, window)` are shared.

## Cache Invalidation

`SharedPubSubManager.handle_message()` upserts the candle into `SharedCandleCache`, then dispatches the event to strategy queues. Invalidation happens in the same event processing chain:

1. `SharedCandleCache.upsert_candle()` (existing)
2. `SharedPanelCache.invalidate(tf)` — mark dirty, rebuild on next access
3. `FeatureCache.invalidate(tf)` — drop all features for TF X

The pubsub manager calls `panel_cache.invalidate(event.tf)` and `feature_cache.invalidate(event.tf)` after upserting the candle but before dispatching events to strategies. This ensures the first strategy to call `get_panel()` after invalidation triggers a single rebuild; subsequent strategies get the cached result.

Lazy rebuild: when `get_panel(tf)` is called on a dirty TF, the panel is rebuilt from SharedCandleCache, marked clean, and returned. In asyncio (single-threaded), there is no race condition — only one coroutine executes at a time.

## Feature Key Derivation from Spec

Each signal type declares feature dependencies deterministically. The mask key `(universe_mode, universe_size)` is appended to each feature key.

| Signal | Feature Keys (mask key omitted for brevity) |
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

### Special transforms

- **`decay`**: Two-step transform. `decay(zscore(field, z_window), d)` depends on `zscore(field, z_window)` as intermediate. When computing `decay`, the FeatureCache first ensures `zscore(field, z_window)` is cached, then applies `_decay_linear` on it. Both intermediate and final are cached.
- **`range_location`**: Custom transform using `rolling().min()/max()` of `low`/`high` fields. Key: `(tf, "close", "range_location", range_window)`. Input: `close`, `high`, `low` from the panel (not a single field). Implemented as a dedicated transform function in FeatureCache.

## Integration with CrossSectionalRunnerStrategy

Before (each alpha builds independently):
```python
panel = build_panel(snapshot)
selection = select_positions(panel, self.spec)
```

After (shared cache):
```python
panel = self.ctx.panel_cache.get_panel(self.spec.timeframe, self.ctx.cache, self.spec)
features = self.ctx.feature_cache.compute_features(panel, self.spec)
selection = select_positions_from_features(features, self.spec)
```

`SharedPanelCache.get_panel(tf, cache, spec)` builds the panel from SharedCandleCache if dirty. The `spec` argument provides the universe mask for `dynamic_top_k` alphas — but the **unmasked** panel is shared; masking is applied as a view/wrapper per alpha, not as separate panel copies.

`select_positions_from_features` replaces `compute_signal_details` — uses cached features instead of recomputing. It still applies `_cs_zscore` (cross-sectional z-score) and combines components, since those are lightweight row operations on the latest bar only.

The existing `select_positions` and `compute_signal_details` remain unchanged for backward compatibility (standalone alphas outside the runner).

## Files Changed

| File | Change |
|------|--------|
| `cross_alpha/panel_cache.py` (new) | `SharedPanelCache` class — builds panel from SharedCandleCache, lazy invalidation |
| `cross_alpha/feature_cache.py` (new) | `FeatureCache` class + key derivation + transform dispatch |
| `cross_alpha/strategy.py` | Add `select_positions_from_features()`, keep backward compat |
| `runner/strategy/context.py` | Add `panel_cache`, `feature_cache` fields |
| `runner/main.py` | Init `SharedPanelCache`, `FeatureCache`, pass to context; call invalidate in pubsub handler |

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

5 alphas on 15m receive the same candle event. All use `dynamic_top_k, 180`:

Without cache: 5 × build_panel + 5 × rolling (with overlaps)
With cache: 1 × build_panel + 6 unique rolling features (zscore_close_5760, skew_returns_3840, zscore_close_8640, zscore_vwap_8640, mean_returns_1920, momentum_close_1920 / std_returns_1920)

Shared features:
- `zscore(close, 5760)`: used by 15m-blend-close and 15m-blend-close-c
- `skew(returns, 3840)`: used by 15m-blend-close and 15m-blend-close-b

## Concrete Example: 4h Group (mixed mask)

2 alphas use `current_top_k, 60` and 1 uses `dynamic_top_k, 180`:

- `4h-trend-close` and `4h-trend-vwap`: share `zscore(close/vwap, 540)` computed on **unmasked** data
- `4h-momentum-vwap`: uses `momentum(vwap, 90)` computed on **masked** data (different cache key)
- No cross-mask sharing for rolling features, but the **unmasked panel** is still shared
