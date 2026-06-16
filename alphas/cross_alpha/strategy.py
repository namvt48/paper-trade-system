from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from cross_alpha.spec import AlphaSpec


@dataclass
class Selection:
    longs: list[str]
    shorts: list[str]
    scores: dict[str, float]
    ranks: dict[str, float]
    weights: dict[str, float]
    indicators: dict[str, dict[str, float | bool | None]]
    diagnostics: dict[str, Any]


def _ts_mean(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(1, d // 2)).mean()


def _ts_std(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(2, d // 2)).std()


def _ts_zscore(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return (x - _ts_mean(x, d)) / _ts_std(x, d).replace(0, np.nan)


def _ts_skew(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(3, d // 2)).skew()


def _ts_momentum(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x / x.shift(d) - 1.0


def _decay_linear(x: pd.DataFrame, d: int) -> pd.DataFrame:
    weights = np.arange(d, 0, -1, dtype=float)
    weights /= weights.sum()
    return sum(weights[k] * x.shift(k) for k in range(d))


def _cs_zscore(x: pd.DataFrame) -> pd.DataFrame:
    return x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1).replace(0, np.nan), axis=0)


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
) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None, dict[str, pd.DataFrame]]:
    raw_returns = panel["close"].pct_change(fill_method=None)
    fields = _masked_fields(panel, spec)
    p = spec.params
    signal = spec.signal
    returns = raw_returns.where(fields["close"].notna()) if spec.universe_mode == "dynamic_top_k" else raw_returns

    if signal == "zscore":
        score = _ts_zscore(_field(fields, p["field"]), p["window"])
        return score, None, None, {"zscore": score}
    if signal == "momentum":
        score = _ts_momentum(_field(fields, p["field"]), p["window"])
        return score, None, None, {"momentum": score}
    if signal == "decay_zscore":
        zscore = _ts_zscore(_field(fields, p["field"]), p["z_window"])
        score = _decay_linear(zscore, p["decay"])
        return score, None, None, {"zscore": zscore, "decay_zscore": score}
    if signal == "blend_zscore_skew":
        price_zscore = _ts_zscore(fields["close"], p["z_window"])
        negative_skew = -_ts_skew(returns, p["skew_window"])
        price_component = _cs_zscore(price_zscore)
        skew_component = _cs_zscore(negative_skew)
        return price_component + skew_component, None, None, {
            "price_zscore": price_zscore, "price_component": price_component,
            "negative_skew": negative_skew, "skew_component": skew_component,
        }
    if signal == "blend_momvol_skew":
        momvol = _ts_momentum(fields["close"], p["momentum_window"]) / _ts_std(returns, p["std_window"])
        negative_skew = -_ts_skew(returns, p["skew_window"])
        momvol_component = _cs_zscore(momvol)
        skew_component = _cs_zscore(negative_skew)
        return momvol_component + skew_component, None, None, {
            "momentum_over_vol": momvol, "momvol_component": momvol_component,
            "negative_skew": negative_skew, "skew_component": skew_component,
        }
    if signal == "blend_zscore_meanret":
        price_zscore = _ts_zscore(fields["close"], p["z_window"])
        mean_return = _ts_mean(returns, p["mean_window"])
        price_component = _cs_zscore(price_zscore)
        return_component = _cs_zscore(mean_return)
        return price_component + return_component, None, None, {
            "price_zscore": price_zscore, "price_component": price_component,
            "mean_return": mean_return, "return_component": return_component,
        }
    if signal == "blend_zscore_momvol":
        momvol = _ts_momentum(fields["close"], p["momentum_window"]) / _ts_std(returns, p["std_window"])
        price_zscore = _ts_zscore(fields["close"], p["z_window"])
        price_component = _cs_zscore(price_zscore)
        momvol_component = _cs_zscore(momvol)
        return price_component + momvol_component, None, None, {
            "price_zscore": price_zscore, "price_component": price_component,
            "momentum_over_vol": momvol, "momvol_component": momvol_component,
        }
    if signal == "blend_zscore_decayz":
        first = _ts_zscore(fields["close"], p["first_z_window"])
        second = _decay_linear(_ts_zscore(fields["close"], p["second_z_window"]), p["decay"])
        first_component = _cs_zscore(first)
        second_component = _cs_zscore(second)
        return first_component + second_component, None, None, {
            "first_zscore": first, "first_component": first_component,
            "decay_zscore": second, "decay_component": second_component,
        }
    if signal == "blend_decayz_meanret":
        first = _decay_linear(_ts_zscore(fields["close"], p["z_window"]), p["decay"])
        mean_return = _ts_mean(returns, p["mean_window"])
        decay_component = _cs_zscore(first)
        return_component = _cs_zscore(mean_return)
        return decay_component + return_component, None, None, {
            "decay_zscore": first, "decay_component": decay_component,
            "mean_return": mean_return, "return_component": return_component,
        }
    if signal == "blend_meanret_range":
        lo = fields["low"].rolling(p["range_window"], min_periods=1).min()
        hi = fields["high"].rolling(p["range_window"], min_periods=1).max()
        location = (fields["close"] - lo) / (hi - lo).replace(0, np.nan)
        mean_return = _ts_mean(returns, p["mean_window"])
        return_component = _cs_zscore(mean_return)
        range_component = _cs_zscore(location)
        return return_component + range_component, None, None, {
            "mean_return": mean_return, "return_component": return_component,
            "range_location": location, "range_component": range_component,
        }
    if signal == "blend_zscore_volume_zscore":
        close_zscore = _ts_zscore(fields["close"], p["close_window"])
        volume_zscore = _ts_zscore(fields["volume"], p["volume_window"])
        close_component = _cs_zscore(close_zscore)
        volume_component = _cs_zscore(volume_zscore)
        return close_component + volume_component, None, None, {
            "close_zscore": close_zscore, "close_component": close_component,
            "volume_zscore": volume_zscore, "volume_component": volume_component,
        }
    if signal == "blend_decayz_volume_zscore":
        first = _decay_linear(_ts_zscore(fields["close"], p["close_window"]), p["decay"])
        volume_zscore = _ts_zscore(fields["volume"], p["volume_window"])
        decay_component = _cs_zscore(first)
        volume_component = _cs_zscore(volume_zscore)
        return decay_component + volume_component, None, None, {
            "decay_zscore": first, "decay_component": decay_component,
            "volume_zscore": volume_zscore, "volume_component": volume_component,
        }
    if signal == "absolute_breakout":
        long_zscore = _ts_zscore(_field(fields, p["long_field"]), p["long_window"])
        short_zscore = _ts_zscore(_field(fields, p["short_field"]), p["short_window"])
        long_condition = long_zscore > p["long_z"]
        short_condition = short_zscore < p["short_z"]
        return None, long_condition, short_condition, {
            "long_zscore": long_zscore, "short_zscore": short_zscore,
        }
    raise ValueError(f"Unsupported signal: {signal}")


def compute_score(panel: dict[str, pd.DataFrame], spec: AlphaSpec) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None]:
    score, long_condition, short_condition, _ = compute_signal_details(panel, spec)
    return score, long_condition, short_condition


def select_positions(panel: dict[str, pd.DataFrame], spec: AlphaSpec) -> Selection:
    score, long_condition, short_condition, components = compute_signal_details(panel, spec)
    scores: dict[str, float] = {}
    ranks: dict[str, float] = {}

    if score is not None:
        latest = score.iloc[-1].dropna()
        rank = latest.rank(pct=True)
        scores = {str(k): float(v) for k, v in latest.items()}
        ranks = {str(k): float(v) for k, v in rank.items()}
        longs = [str(symbol) for symbol, value in rank.items() if value > float(spec.long_threshold)]
        shorts = [str(symbol) for symbol, value in rank.items() if value < float(spec.short_threshold)]
    else:
        assert long_condition is not None and short_condition is not None
        latest_long = long_condition.iloc[-1].fillna(False)
        latest_short = short_condition.iloc[-1].fillna(False)
        longs = [str(symbol) for symbol, active in latest_long.items() if bool(active)]
        shorts = [str(symbol) for symbol, active in latest_short.items() if bool(active)]

    conflict = set(longs) & set(shorts)
    longs = sorted(set(longs) - conflict)
    shorts = sorted(set(shorts) - conflict)
    if spec.universe_mode == "current_top_k":
        liquidity = (panel["close"] * panel["volume"]).rolling(30, min_periods=1).mean().iloc[-1]
        liquidity_ranks = liquidity.rank(ascending=False)
        eligible = set(liquidity_ranks[liquidity_ranks <= spec.universe_size].index.astype(str))
        longs = [symbol for symbol in longs if symbol in eligible]
        shorts = [symbol for symbol in shorts if symbol in eligible]

    # The prose spec requires equal-weight legs and dollar neutrality. Stay flat
    # when only one leg is available rather than silently taking market beta.
    weights: dict[str, float] = {}
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
