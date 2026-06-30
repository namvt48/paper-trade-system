from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from cross_alpha.spec import AlphaSpec
from indicators.pandas.ts_ops import ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear, ts_range_location, ts_range_location_close
from indicators.pandas.cs_ops import cs_zscore as _cs_zscore_lib, cs_winsorize as _cs_winsorize_lib, cs_scale as _cs_scale_lib
from indicators.pandas.element_ops import abs as _abs_lib, neg as _neg_lib, add as _add_lib, div as _div_lib


@dataclass
class Selection:
    longs: list[str]
    shorts: list[str]
    scores: dict[str, float]
    ranks: dict[str, float]
    weights: dict[str, float]
    indicators: dict[str, dict[str, float | bool | None]]
    diagnostics: dict[str, Any]


FeatureKey = tuple[Any, ...]


class CrossAlphaComputeContext:
    """Per-panel cache shared by alpha specs evaluated on the same candle."""

    def __init__(self, panel: dict[str, pd.DataFrame], metrics: dict[str, int | float] | None = None):
        self.panel = panel
        self.metrics = metrics
        self._raw_returns: pd.DataFrame | None = None
        self._liquidity_rank: pd.DataFrame | None = None
        self._masked_fields: dict[FeatureKey, dict[str, pd.DataFrame]] = {}
        self._returns: dict[FeatureKey, pd.DataFrame] = {}
        self._features: dict[FeatureKey, pd.DataFrame] = {}

    def raw_returns(self) -> pd.DataFrame:
        if self._raw_returns is None:
            self._raw_returns = self.panel["close"].pct_change(fill_method=None)
        return self._raw_returns

    def liquidity_rank(self) -> pd.DataFrame:
        if self._liquidity_rank is None:
            dollar_volume = self.panel["close"] * self.panel["volume"]
            self._liquidity_rank = dollar_volume.rolling(30, min_periods=1).mean().rank(axis=1, ascending=False)
        return self._liquidity_rank

    def mask_key(self, spec: AlphaSpec) -> FeatureKey:
        if spec.universe_mode == "dynamic_top_k":
            return ("dynamic_top_k", int(spec.universe_size))
        return ("all",)

    def masked_fields(self, spec: AlphaSpec) -> tuple[dict[str, pd.DataFrame], FeatureKey]:
        key = self.mask_key(spec)
        if key not in self._masked_fields:
            if spec.universe_mode == "dynamic_top_k":
                mask = self.liquidity_rank() <= int(spec.universe_size)
                self._masked_fields[key] = {name: value.where(mask) for name, value in self.panel.items()}
            else:
                self._masked_fields[key] = self.panel
        return self._masked_fields[key], key

    def returns(self, spec: AlphaSpec, fields: dict[str, pd.DataFrame], mask_key: FeatureKey) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("returns", mask_key)
        if key not in self._returns:
            raw = self.raw_returns()
            if spec.universe_mode == "dynamic_top_k":
                self._returns[key] = raw.where(fields["close"].notna())
            else:
                self._returns[key] = raw
        return self._returns[key], key

    def field(self, fields: dict[str, pd.DataFrame], mask_key: FeatureKey, name: str) -> tuple[pd.DataFrame, FeatureKey]:
        return fields[name], ("field", mask_key, name)

    def ts_mean(self, source_key: FeatureKey, x: pd.DataFrame, d: int) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("ts_mean", source_key, int(d))
        if key not in self._features:
            self._inc("feature_cache_misses")
            self._features[key] = ts_mean(x, int(d))
        else:
            self._inc("feature_cache_hits")
        return self._features[key], key

    def ts_std(self, source_key: FeatureKey, x: pd.DataFrame, d: int) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("ts_std", source_key, int(d))
        if key not in self._features:
            self._inc("feature_cache_misses")
            self._features[key] = ts_std(x, int(d))
        else:
            self._inc("feature_cache_hits")
        return self._features[key], key

    def ts_zscore(self, source_key: FeatureKey, x: pd.DataFrame, d: int) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("ts_zscore", source_key, int(d))
        if key not in self._features:
            self._inc("feature_cache_misses")
            self._features[key] = ts_zscore(x, int(d))
        else:
            self._inc("feature_cache_hits")
        return self._features[key], key

    def ts_skew(self, source_key: FeatureKey, x: pd.DataFrame, d: int) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("ts_skew", source_key, int(d))
        if key not in self._features:
            self._inc("feature_cache_misses")
            self._features[key] = ts_skew(x, int(d))
        else:
            self._inc("feature_cache_hits")
        return self._features[key], key

    def ts_momentum(self, source_key: FeatureKey, x: pd.DataFrame, d: int) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("ts_momentum", source_key, int(d))
        if key not in self._features:
            self._inc("feature_cache_misses")
            self._features[key] = ts_momentum(x, int(d))
        else:
            self._inc("feature_cache_hits")
        return self._features[key], key

    def decay_linear(self, source_key: FeatureKey, x: pd.DataFrame, d: int) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("decay_linear", source_key, int(d))
        if key not in self._features:
            self._inc("feature_cache_misses")
            self._features[key] = decay_linear(x, int(d))
        else:
            self._inc("feature_cache_hits")
        return self._features[key], key

    def cs_zscore(self, source_key: FeatureKey, x: pd.DataFrame) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("cs_zscore", source_key)
        return _cs_zscore_lib(x), key

    def cs_winsorize(self, source_key: FeatureKey, x: pd.DataFrame, k: float = 3.0) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("cs_winsorize", source_key, float(k))
        return _cs_winsorize_lib(x, k), key

    def cs_scale(self, source_key: FeatureKey, x: pd.DataFrame, a: float = 1.0) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("cs_scale", source_key, float(a))
        return _cs_scale_lib(x, a), key

    def neg(self, source_key: FeatureKey, x: pd.DataFrame) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("neg", source_key)
        return _neg_lib(x), key

    def abs(self, source_key: FeatureKey, x: pd.DataFrame) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("abs", source_key)
        return _abs_lib(x), key

    def div(
        self,
        left_key: FeatureKey,
        left: pd.DataFrame,
        right_key: FeatureKey,
        right: pd.DataFrame,
    ) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("div", left_key, right_key)
        return _div_lib(left, right), key

    def add(
        self,
        left_key: FeatureKey,
        left: pd.DataFrame,
        right_key: FeatureKey,
        right: pd.DataFrame,
    ) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("add", left_key, right_key)
        return _add_lib(left, right), key

    def range_location(self, fields: dict[str, pd.DataFrame], mask_key: FeatureKey, window: int) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("range_location", mask_key, int(window))
        if key not in self._features:
            self._inc("feature_cache_misses")
            self._features[key] = ts_range_location(fields["close"], fields["low"], fields["high"], int(window))
        else:
            self._inc("feature_cache_hits")
        return self._features[key], key

    def range_location_close(self, fields: dict[str, pd.DataFrame], mask_key: FeatureKey, window: int) -> tuple[pd.DataFrame, FeatureKey]:
        key = ("range_location_close", mask_key, int(window))
        if key not in self._features:
            self._inc("feature_cache_misses")
            self._features[key] = ts_range_location_close(fields["close"], int(window))
        else:
            self._inc("feature_cache_hits")
        return self._features[key], key

    def _inc(self, name: str) -> None:
        if self.metrics is None:
            return
        self.metrics[name] = int(self.metrics.get(name, 0)) + 1


def _masked_fields(panel: dict[str, pd.DataFrame], spec: AlphaSpec) -> dict[str, pd.DataFrame]:
    dollar_volume = panel["close"] * panel["volume"]
    liq_rank = dollar_volume.rolling(30, min_periods=1).mean().rank(axis=1, ascending=False)
    mask = liq_rank <= spec.universe_size
    if spec.universe_mode == "dynamic_top_k":
        return {name: value.where(mask) for name, value in panel.items()}
    return panel


def _field(fields: dict[str, pd.DataFrame], name: str) -> pd.DataFrame:
    return fields[name]


def _audit_value(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


def compute_signal_details(
    panel: dict[str, pd.DataFrame],
    spec: AlphaSpec,
    context: CrossAlphaComputeContext | None = None,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None, dict[str, pd.DataFrame]]:
    ctx = context or CrossAlphaComputeContext(panel)
    fields, mask_key = ctx.masked_fields(spec)
    p = spec.params
    signal = spec.signal
    returns, returns_key = ctx.returns(spec, fields, mask_key)

    if signal == "zscore":
        field, field_key = ctx.field(fields, mask_key, p["field"])
        score, _ = ctx.ts_zscore(field_key, field, p["window"])
        return score, None, None, {"zscore": score}
    if signal == "momentum":
        field, field_key = ctx.field(fields, mask_key, p["field"])
        score, _ = ctx.ts_momentum(field_key, field, p["window"])
        return score, None, None, {"momentum": score}
    if signal == "decay_zscore":
        field, field_key = ctx.field(fields, mask_key, p["field"])
        zscore, zscore_key = ctx.ts_zscore(field_key, field, p["z_window"])
        score, _ = ctx.decay_linear(zscore_key, zscore, p["decay"])
        return score, None, None, {"zscore": zscore, "decay_zscore": score}
    if signal == "blend_zscore_skew":
        close, close_key = ctx.field(fields, mask_key, "close")
        price_zscore, price_zscore_key = ctx.ts_zscore(close_key, close, p["z_window"])
        skew, skew_key = ctx.ts_skew(returns_key, returns, p["skew_window"])
        negative_skew, negative_skew_key = ctx.neg(skew_key, skew)
        price_component, price_component_key = ctx.cs_zscore(price_zscore_key, price_zscore)
        skew_component, skew_component_key = ctx.cs_zscore(negative_skew_key, negative_skew)
        score, _ = ctx.add(price_component_key, price_component, skew_component_key, skew_component)
        return score, None, None, {
            "price_zscore": price_zscore, "price_component": price_component,
            "negative_skew": negative_skew, "skew_component": skew_component,
        }
    if signal == "blend_momvol_skew":
        close, close_key = ctx.field(fields, mask_key, "close")
        momentum, momentum_key = ctx.ts_momentum(close_key, close, p["momentum_window"])
        std, std_key = ctx.ts_std(returns_key, returns, p["std_window"])
        momvol, momvol_key = ctx.div(momentum_key, momentum, std_key, std)
        skew, skew_key = ctx.ts_skew(returns_key, returns, p["skew_window"])
        negative_skew, negative_skew_key = ctx.neg(skew_key, skew)
        momvol_component, momvol_component_key = ctx.cs_zscore(momvol_key, momvol)
        skew_component, skew_component_key = ctx.cs_zscore(negative_skew_key, negative_skew)
        score, _ = ctx.add(momvol_component_key, momvol_component, skew_component_key, skew_component)
        return score, None, None, {
            "momentum_over_vol": momvol, "momvol_component": momvol_component,
            "negative_skew": negative_skew, "skew_component": skew_component,
        }
    if signal == "blend_zscore_meanret":
        close, close_key = ctx.field(fields, mask_key, "close")
        price_zscore, price_zscore_key = ctx.ts_zscore(close_key, close, p["z_window"])
        mean_return, mean_return_key = ctx.ts_mean(returns_key, returns, p["mean_window"])
        price_component, price_component_key = ctx.cs_zscore(price_zscore_key, price_zscore)
        return_component, return_component_key = ctx.cs_zscore(mean_return_key, mean_return)
        score, _ = ctx.add(price_component_key, price_component, return_component_key, return_component)
        return score, None, None, {
            "price_zscore": price_zscore, "price_component": price_component,
            "mean_return": mean_return, "return_component": return_component,
        }
    if signal == "blend_zscore_momvol":
        close, close_key = ctx.field(fields, mask_key, "close")
        momentum, momentum_key = ctx.ts_momentum(close_key, close, p["momentum_window"])
        std, std_key = ctx.ts_std(returns_key, returns, p["std_window"])
        momvol, momvol_key = ctx.div(momentum_key, momentum, std_key, std)
        price_zscore, price_zscore_key = ctx.ts_zscore(close_key, close, p["z_window"])
        price_component, price_component_key = ctx.cs_zscore(price_zscore_key, price_zscore)
        momvol_component, momvol_component_key = ctx.cs_zscore(momvol_key, momvol)
        score, _ = ctx.add(price_component_key, price_component, momvol_component_key, momvol_component)
        return score, None, None, {
            "price_zscore": price_zscore, "price_component": price_component,
            "momentum_over_vol": momvol, "momvol_component": momvol_component,
        }
    if signal == "blend_zscore_decayz":
        close, close_key = ctx.field(fields, mask_key, "close")
        first, first_key = ctx.ts_zscore(close_key, close, p["first_z_window"])
        second_zscore, second_zscore_key = ctx.ts_zscore(close_key, close, p["second_z_window"])
        second, second_key = ctx.decay_linear(second_zscore_key, second_zscore, p["decay"])
        first_component, first_component_key = ctx.cs_zscore(first_key, first)
        second_component, second_component_key = ctx.cs_zscore(second_key, second)
        score, _ = ctx.add(first_component_key, first_component, second_component_key, second_component)
        return score, None, None, {
            "first_zscore": first, "first_component": first_component,
            "decay_zscore": second, "decay_component": second_component,
        }
    if signal == "blend_decayz_meanret":
        close, close_key = ctx.field(fields, mask_key, "close")
        zscore, zscore_key = ctx.ts_zscore(close_key, close, p["z_window"])
        first, first_key = ctx.decay_linear(zscore_key, zscore, p["decay"])
        mean_return, mean_return_key = ctx.ts_mean(returns_key, returns, p["mean_window"])
        decay_component, decay_component_key = ctx.cs_zscore(first_key, first)
        return_component, return_component_key = ctx.cs_zscore(mean_return_key, mean_return)
        score, _ = ctx.add(decay_component_key, decay_component, return_component_key, return_component)
        return score, None, None, {
            "decay_zscore": first, "decay_component": decay_component,
            "mean_return": mean_return, "return_component": return_component,
        }
    if signal == "blend_meanret_range":
        location, location_key = ctx.range_location(fields, mask_key, p["range_window"])
        mean_return, mean_return_key = ctx.ts_mean(returns_key, returns, p["mean_window"])
        return_component, return_component_key = ctx.cs_zscore(mean_return_key, mean_return)
        range_component, range_component_key = ctx.cs_zscore(location_key, location)
        score, _ = ctx.add(return_component_key, return_component, range_component_key, range_component)
        return score, None, None, {
            "mean_return": mean_return, "return_component": return_component,
            "range_location": location, "range_component": range_component,
        }
    if signal == "blend_zscore_volume_zscore":
        close, close_key = ctx.field(fields, mask_key, "close")
        volume, volume_key = ctx.field(fields, mask_key, "volume")
        close_zscore, close_zscore_key = ctx.ts_zscore(close_key, close, p["close_window"])
        volume_zscore, volume_zscore_key = ctx.ts_zscore(volume_key, volume, p["volume_window"])
        close_component, close_component_key = ctx.cs_zscore(close_zscore_key, close_zscore)
        volume_component, volume_component_key = ctx.cs_zscore(volume_zscore_key, volume_zscore)
        score, _ = ctx.add(close_component_key, close_component, volume_component_key, volume_component)
        return score, None, None, {
            "close_zscore": close_zscore, "close_component": close_component,
            "volume_zscore": volume_zscore, "volume_component": volume_component,
        }
    if signal == "blend_decayz_volume_zscore":
        close, close_key = ctx.field(fields, mask_key, "close")
        volume, volume_key = ctx.field(fields, mask_key, "volume")
        close_zscore, close_zscore_key = ctx.ts_zscore(close_key, close, p["close_window"])
        first, first_key = ctx.decay_linear(close_zscore_key, close_zscore, p["decay"])
        volume_zscore, volume_zscore_key = ctx.ts_zscore(volume_key, volume, p["volume_window"])
        decay_component, decay_component_key = ctx.cs_zscore(first_key, first)
        volume_component, volume_component_key = ctx.cs_zscore(volume_zscore_key, volume_zscore)
        score, _ = ctx.add(decay_component_key, decay_component, volume_component_key, volume_component)
        return score, None, None, {
            "decay_zscore": first, "decay_component": decay_component,
            "volume_zscore": volume_zscore, "volume_component": volume_component,
        }
    if signal == "absolute_breakout":
        long_field, long_field_key = ctx.field(fields, mask_key, p["long_field"])
        short_field, short_field_key = ctx.field(fields, mask_key, p["short_field"])
        long_zscore, _ = ctx.ts_zscore(long_field_key, long_field, p["long_window"])
        short_zscore, _ = ctx.ts_zscore(short_field_key, short_field, p["short_window"])
        long_condition = long_zscore > p["long_z"]
        short_condition = short_zscore < p["short_z"]
        return None, long_condition, short_condition, {
            "long_zscore": long_zscore, "short_zscore": short_zscore,
        }
    if signal == "breakout":
        location, location_key = ctx.range_location_close(fields, mask_key, p["window"])
        return location, None, None, {"range_location_close": location}
    if signal == "breakout_hl":
        location, location_key = ctx.range_location(fields, mask_key, p["window"])
        return location, None, None, {"range_location": location}
    if signal == "blend_zscore_range":
        close, close_key = ctx.field(fields, mask_key, "close")
        price_zscore, price_zscore_key = ctx.ts_zscore(close_key, close, p["z_window"])
        location, location_key = ctx.range_location_close(fields, mask_key, p["range_window"])
        price_component, price_component_key = ctx.cs_zscore(price_zscore_key, price_zscore)
        range_component, range_component_key = ctx.cs_zscore(location_key, location)
        score, _ = ctx.add(price_component_key, price_component, range_component_key, range_component)
        return score, None, None, {
            "price_zscore": price_zscore, "price_component": price_component,
            "range_location_close": location, "range_component": range_component,
        }
    if signal == "amihud":
        abs_returns, abs_returns_key = ctx.abs(returns_key, returns)
        quote_volume, quote_volume_key = ctx.field(fields, mask_key, "quote_volume")
        illiq, illiq_key = ctx.div(abs_returns_key, abs_returns, quote_volume_key, quote_volume)
        score, _ = ctx.ts_mean(illiq_key, illiq, p["window"])
        return score, None, None, {"abs_returns": abs_returns, "illiquidity": illiq, "amihud": score}
    raise ValueError(f"Unsupported signal: {signal}")


def compute_score(
    panel: dict[str, pd.DataFrame],
    spec: AlphaSpec,
    context: CrossAlphaComputeContext | None = None,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None]:
    score, long_condition, short_condition, _ = compute_signal_details(panel, spec, context=context)
    return score, long_condition, short_condition


def select_positions(
    panel: dict[str, pd.DataFrame],
    spec: AlphaSpec,
    context: CrossAlphaComputeContext | None = None,
) -> Selection:
    ctx = context or CrossAlphaComputeContext(panel)
    score, long_condition, short_condition, components = compute_signal_details(panel, spec, context=ctx)
    scores: dict[str, float] = {}
    ranks: dict[str, float] = {}

    weights: dict[str, float] = {}

    if score is not None:
        latest = score.iloc[-1].dropna()

        if spec.construction == "winsor_cont":
            # Winsor-cont: weight ∝ cs_scale(cs_winsorize(cs_zscore(signal), k))
            # Sized by magnitude — stronger signal → bigger position, clipped at ±kσ.
            k = spec.winsor_k
            zscored = latest.sub(latest.mean()).div(latest.std() if latest.std() != 0 else np.nan)
            winsorized = zscored.clip(lower=-k, upper=k)
            gross_abs = winsorized.abs().sum()
            if gross_abs > 0:
                scaled = winsorized / gross_abs
            else:
                scaled = winsorized
            scores = {str(k_): float(v) for k_, v in latest.items()}
            ranks = {str(k_): float(v) for k_, v in latest.rank(pct=True).items()}
            longs = sorted(str(s) for s, w in scaled.items() if w > 0)
            shorts = sorted(str(s) for s, w in scaled.items() if w < 0)
            weights = {str(s): float(w) for s, w in scaled.items() if w != 0}
        else:
            n_long = max(1, round(len(latest) * (1 - float(spec.long_threshold))))
            n_short = max(1, round(len(latest) * float(spec.short_threshold)))
            k = min(n_long, n_short)
            top_k = latest.nlargest(k)
            bottom_k = latest.nsmallest(k)
            rank = latest.rank(pct=True)
            scores = {str(sym): float(val) for sym, val in latest.items()}
            ranks = {str(sym): float(val) for sym, val in rank.items()}
            longs = [str(sym) for sym in top_k.index]
            shorts = [str(sym) for sym in bottom_k.index]
    else:
        assert long_condition is not None and short_condition is not None
        latest_long = long_condition.iloc[-1].fillna(False)
        latest_short = short_condition.iloc[-1].fillna(False)
        longs = [str(symbol) for symbol, active in latest_long.items() if bool(active)]
        shorts = [str(symbol) for symbol, active in latest_short.items() if bool(active)]

    if spec.construction == "winsor_cont":
        if spec.universe_mode == "current_top_k":
            liquidity = (panel["close"] * panel["volume"]).rolling(30, min_periods=1).mean().iloc[-1]
            liquidity_ranks = liquidity.rank(ascending=False)
            eligible = set(liquidity_ranks[liquidity_ranks <= spec.universe_size].index.astype(str))
            weights = {symbol: weight for symbol, weight in weights.items() if symbol in eligible}
        longs = sorted(symbol for symbol, weight in weights.items() if weight > 0)
        shorts = sorted(symbol for symbol, weight in weights.items() if weight < 0)
        # Balance LONG = SHORT: trim the larger side down to the smaller,
        # removing the weakest signals first (lowest |weight|).  Without this
        # winsor_cont produces unequal long/short counts, leaving an odd total
        # and an unintended directional bias.
        if longs and shorts and len(longs) != len(shorts):
            _target = min(len(longs), len(shorts))
            if len(longs) > _target:
                _drop = set(sorted(longs, key=lambda s: weights[s])[: len(longs) - _target])
                weights = {s: w for s, w in weights.items() if s not in _drop}
                longs = sorted(s for s in longs if s not in _drop)
            else:
                _drop = set(sorted(shorts, key=lambda s: abs(weights[s]))[: len(shorts) - _target])
                weights = {s: w for s, w in weights.items() if s not in _drop}
                shorts = sorted(s for s in shorts if s not in _drop)
            # Re-normalise so gross |weights| ≈ 1 (same scale as before trim).
            _gross = sum(abs(w) for w in weights.values())
            if _gross > 0:
                weights = {s: w / _gross for s, w in weights.items()}
    else:
        conflict = set(longs) & set(shorts)
        longs = sorted(set(longs) - conflict)
        shorts = sorted(set(shorts) - conflict)
        if spec.universe_mode == "current_top_k":
            liquidity = (panel["close"] * panel["volume"]).rolling(30, min_periods=1).mean().iloc[-1]
            liquidity_ranks = liquidity.rank(ascending=False)
            eligible = set(liquidity_ranks[liquidity_ranks <= spec.universe_size].index.astype(str))
            longs = [symbol for symbol in longs if symbol in eligible]
            shorts = [symbol for symbol in shorts if symbol in eligible]

        k = min(len(longs), len(shorts))
        longs = longs[:k]
        shorts = shorts[:k]

        if longs and shorts:
            weights.update({symbol: 0.5 / len(longs) for symbol in longs})
            weights.update({symbol: -0.5 / len(shorts) for symbol in shorts})

    all_symbols = sorted(set(panel["close"].columns.astype(str)))
    indicators: dict[str, dict[str, float | bool | None]] = {}
    for symbol in all_symbols:
        row: dict[str, Any] = {}
        for name, values in components.items():
            value = values[symbol].iloc[-1] if symbol in values.columns and not values.empty else np.nan
            row[name] = _audit_value(value)
        row["score"] = _audit_value(scores.get(symbol))
        row["rank"] = _audit_value(ranks.get(symbol))
        row["decision"] = "LONG" if symbol in longs else "SHORT" if symbol in shorts else "FLAT"
        row["target_weight"] = weights.get(symbol, 0.0)
        indicators[symbol] = row

    return Selection(
        longs=longs,
        shorts=shorts,
        scores=scores,
        ranks=ranks,
        weights=weights,
        indicators=indicators,
        diagnostics={
            "gross": sum(abs(v) for v in weights.values()),
            "net": sum(weights.values()),
            "vwap_source": "typical_price_proxy",
            "liquidity_source": "close_x_base_volume_proxy",
        },
    )


def build_panel(snapshot: dict[str, dict[str, list[float] | list[int]]]) -> dict[str, pd.DataFrame]:
    def make(field: str) -> pd.DataFrame:
        series = {}
        for symbol, row in snapshot.items():
            values = row[field]
            times = row["time"]
            if values and times and len(values) == len(times):
                series[symbol] = pd.Series(values, index=pd.Index(times, dtype="int64"), dtype="float64")
        return pd.DataFrame(series).sort_index()

    close = make("close")
    high = make("high").reindex_like(close)
    low = make("low").reindex_like(close)
    volume = make("volume").reindex_like(close)
    # Current MDS does not expose quote volume. The proxy is explicit in
    # diagnostics and validators so VWAP alphas cannot be mistaken for exact.
    vwap = (high + low + close) / 3.0
    return {"close": close, "high": high, "low": low, "volume": volume, "quote_volume": close * volume, "vwap": vwap}
