import statistics
from typing import Optional
import pandas as pd
import pandas_ta as ta

from app.config import settings


def get_candle_seconds(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    elif tf.endswith("h"):
        return int(tf[:-1]) * 3600
    return 60


def get_storage_size_for_tf(tf: str) -> int:
    return {
        "1m": 480, "3m": 200, "5m": 120,
        "15m": 100, "30m": 60, "1h": 100,
    }.get(tf, 100)


def compute_adx(highs: list[float], lows: list[float], closes: list[float], period: int) -> float:
    if len(closes) < period * 2:
        return 0.0
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    adx_df = ta.adx(high=df["high"], low=df["low"], close=df["close"], length=period)
    if adx_df is None or adx_df.empty:
        return 0.0
    col = f"ADX_{period}"
    val = adx_df[col].iloc[-1]
    return float(val) if pd.notna(val) else 0.0


def strategy_filter_signal(
    symbol: str,
    price_list: list[float],
    volume_list: list[float],
    high_list: list[float],
    low_list: list[float],
    btc_price_list: list[float],
    btc_high_list: list[float],
    btc_low_list: list[float],
) -> Optional[dict]:
    if symbol == "BTCUSDT":
        return None

    lb_vol = settings.VOL_LOOKBACK
    lb_price = settings.PRICE_LOOKBACK
    lb_btc = settings.BTC_DIR_LOOKBACK

    min_len = max(lb_vol, lb_price) + 1
    if len(price_list) < min_len or len(volume_list) < min_len:
        return None

    # Step 1: BTC ADX(7) >= 50
    if len(btc_price_list) < settings.ADX_PERIOD * 2:
        return None
    adx_btc = compute_adx(btc_high_list, btc_low_list, btc_price_list, settings.ADX_PERIOD)
    if adx_btc < settings.ADX_THRESHOLD:
        return None

    # Step 2: Volume spike >= 2x median of last N candles
    hist_vols = volume_list[-(lb_vol + 1):-1]
    if not hist_vols:
        return None
    ref_vol = statistics.median(hist_vols)
    if ref_vol <= 0:
        return None
    vol_spike = volume_list[-1] / ref_vol
    if vol_spike < settings.VOL_SPIKE_MIN:
        return None

    # Step 3: |Price Move| 0.8% – 20% vs median of last N candles
    hist_prices = price_list[-(lb_price + 1):-1]
    if not hist_prices:
        return None
    ref_price = statistics.median(hist_prices)
    if ref_price <= 0:
        return None
    price_move = (price_list[-1] - ref_price) / ref_price
    if abs(price_move) < settings.PRICE_MOVE_MIN or abs(price_move) > settings.PRICE_MOVE_MAX:
        return None

    # Step 4: Coin same direction as BTC (last BTC_DIR_LOOKBACK candles)
    if len(btc_price_list) < lb_btc + 1:
        return None
    btc_diff = btc_price_list[-1] - btc_price_list[-(lb_btc + 1)]

    if btc_diff > 0 and price_move > 0:
        recommend = "LONG"
    elif btc_diff < 0 and price_move < 0:
        recommend = "SHORT"
    else:
        return None

    return {
        "symbol": symbol,
        "recommend": recommend,
        "entry": price_list[-1],
        "vol_spike": round(vol_spike, 4),
        "price_move": round(price_move, 6),
        "btc_adx": round(adx_btc, 2),
    }
