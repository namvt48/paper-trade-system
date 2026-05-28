from typing import Optional
import pandas as pd
import pandas_ta as ta

from app.config import settings


def get_candle_seconds(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    elif tf.endswith("h"):
        return int(tf[:-1]) * 3600
    return 3600


def get_storage_size_for_tf(tf: str) -> int:
    return {
        "15m": 100, "30m": 80, "1h": 120, "4h": 60, "1d": 30,
    }.get(tf, 120)


def compute_wilder_indicators(
    high_list: list[float],
    low_list: list[float],
    price_list: list[float],
    rsi_period: int,
    adx_period: int,
    atr_period: int,
    sar_af: float,
    sar_af_step: float,
    sar_af_max: float,
) -> Optional[dict]:
    min_bars = max(rsi_period, adx_period, atr_period) * 3
    if len(price_list) < min_bars:
        return None

    df = pd.DataFrame({
        "high": high_list,
        "low": low_list,
        "close": price_list,
    })

    # ADX + DI+/DI-
    adx_df = ta.adx(high=df["high"], low=df["low"], close=df["close"], length=adx_period)
    if adx_df is None or adx_df.empty:
        return None
    adx_col = f"ADX_{adx_period}"
    dmp_col = f"DMP_{adx_period}"
    dmn_col = f"DMN_{adx_period}"
    if not all(c in adx_df.columns for c in [adx_col, dmp_col, dmn_col]):
        return None
    adx_val = adx_df[adx_col].iloc[-1]
    plus_di = adx_df[dmp_col].iloc[-1]
    minus_di = adx_df[dmn_col].iloc[-1]
    if any(pd.isna(v) for v in [adx_val, plus_di, minus_di]):
        return None

    # RSI
    rsi_ser = ta.rsi(close=df["close"], length=rsi_period)
    if rsi_ser is None or rsi_ser.empty or len(rsi_ser) < 2:
        return None
    rsi_prev = rsi_ser.iloc[-2]
    rsi_curr = rsi_ser.iloc[-1]
    if pd.isna(rsi_prev) or pd.isna(rsi_curr):
        return None

    # ATR
    atr_ser = ta.atr(high=df["high"], low=df["low"], close=df["close"], length=atr_period)
    if atr_ser is None or atr_ser.empty:
        return None
    atr_val = atr_ser.iloc[-1]
    if pd.isna(atr_val) or atr_val <= 0:
        return None

    # Parabolic SAR
    psar_df = ta.psar(
        high=df["high"], low=df["low"], close=df["close"],
        af0=sar_af, af=sar_af_step, max_af=sar_af_max,
    )
    if psar_df is None or psar_df.empty:
        return None

    # Determine SAR trend from PSARl / PSARs columns
    psar_l_col = next((c for c in psar_df.columns if c.startswith("PSARl_")), None)
    psar_s_col = next((c for c in psar_df.columns if c.startswith("PSARs_")), None)
    if psar_l_col is None or psar_s_col is None:
        return None

    sar_l = psar_df[psar_l_col]
    prev_trend = 1 if pd.notna(sar_l.iloc[-2]) else -1
    curr_trend = 1 if pd.notna(sar_l.iloc[-1]) else -1

    return {
        "adx": float(adx_val),
        "plus_di": float(plus_di),
        "minus_di": float(minus_di),
        "rsi_prev": float(rsi_prev),
        "rsi_curr": float(rsi_curr),
        "atr": float(atr_val),
        "sar_trend_prev": prev_trend,
        "sar_trend_curr": curr_trend,
    }


def determine_regime(adx: float) -> str:
    if adx >= settings.TRENDING_THRESHOLD:
        return "TRENDING"
    if adx >= settings.RANGING_THRESHOLD:
        return "TRANSITION"
    return "RANGING"


def wilder_filter_signal(
    symbol: str,
    price_list: list[float],
    high_list: list[float],
    low_list: list[float],
) -> Optional[dict]:
    indic = compute_wilder_indicators(
        high_list=high_list,
        low_list=low_list,
        price_list=price_list,
        rsi_period=settings.RSI_PERIOD,
        adx_period=settings.ADX_PERIOD,
        atr_period=settings.ATR_PERIOD,
        sar_af=settings.SAR_AF_INIT,
        sar_af_step=settings.SAR_AF_STEP,
        sar_af_max=settings.SAR_AF_MAX,
    )
    if indic is None:
        return None

    adx = indic["adx"]
    plus_di = indic["plus_di"]
    minus_di = indic["minus_di"]
    rsi_prev = indic["rsi_prev"]
    rsi_curr = indic["rsi_curr"]
    atr = indic["atr"]
    sar_prev = indic["sar_trend_prev"]
    sar_curr = indic["sar_trend_curr"]
    regime = determine_regime(adx)

    recommend = None

    if regime == "TRENDING":
        sar_flip_up = sar_prev == -1 and sar_curr == 1
        sar_flip_down = sar_prev == 1 and sar_curr == -1
        di_gap = plus_di - minus_di

        if sar_flip_up and plus_di > minus_di and di_gap >= settings.DI_GAP_MIN:
            recommend = "LONG"
        elif sar_flip_down and minus_di > plus_di and (-di_gap) >= settings.DI_GAP_MIN:
            recommend = "SHORT"

    elif regime == "RANGING":
        di_gap = plus_di - minus_di

        # RSI exits oversold → LONG
        if (rsi_prev < settings.RSI_OVERSOLD and rsi_curr >= settings.RSI_OVERSOLD
                and di_gap >= settings.DI_GAP_MIN):
            recommend = "LONG"
        # RSI exits overbought → SHORT
        elif (rsi_prev > settings.RSI_OVERBOUGHT and rsi_curr <= settings.RSI_OVERBOUGHT
              and (-di_gap) >= settings.DI_GAP_MIN):
            recommend = "SHORT"

    if recommend is None:
        return None

    entry = price_list[-1]
    if recommend == "LONG":
        sl = entry - settings.SL_ATR_MULT * atr
        tp = entry + settings.TP_ATR_MULT * atr
    else:
        sl = entry + settings.SL_ATR_MULT * atr
        tp = entry - settings.TP_ATR_MULT * atr

    return {
        "symbol": symbol,
        "recommend": recommend,
        "regime": regime,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "atr": atr,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "rsi_curr": rsi_curr,
    }
