#!/usr/bin/env python3
"""Alpha-1 Backtest V2 (Binance API)
   - Data: Binance REST API (api.binance.com)
   - Note: Can fetch unlimited historical data
"""

import math
import time
import requests
import asyncio
import aiohttp
import os
import pandas as pd
from datetime import datetime, timezone, timedelta

# ── CONFIG ────────────────────────────────────────────

START = datetime.now(timezone.utc) - timedelta(days=30)
END   = datetime.now(timezone.utc)

CAPITAL  = 10_000.0
SIZE     = 1_000.0
MIN_SIZE = 100.0
FEE_RATE = 0.000357  # 0.0357%

EMA_FAST    = 50
EMA_SLOW    = 200
RSI_LEN     = 14
RSI_THRESH  = 40
ATR_LEN     = 14
SL_ATR_MULT = 0.8
TP_RATIO    = 1.2
MAX_HOLD_H  = 24
WARMUP      = 300
SIZING_MODE = "Risk-Based"
RISK_PER_TRADE_PCT = 0.25
LEVERAGE = 1.0
DCA_ENABLED = False
DCA_TRIGGER_R = 0.5
DCA_SIZE_MULT = 0.5
DCA_MOVE_SL_TO_ENTRY = False

TIMEFRAMES = [
    {"id": "15m", "label": "M15", "mins": 15, "binance_interval": "15m"},
    {"id": "1h",  "label": "H1",  "mins": 60, "binance_interval": "1h"},
    {"id": "4h",  "label": "H4",  "mins": 240, "binance_interval": "4h"},
    {"id": "12h", "label": "H12", "mins": 720, "binance_interval": "12h"},
    {"id": "1d",  "label": "D1",  "mins": 1440, "binance_interval": "1d"},
]

STRATEGIES = [
    {
        "id": "v1",
        "label": "v1",
        "use_d1_gate": False,
        "use_reduce50": False,
    },
    {
        "id": "V2_CLV_DG",
        "label": "V2_CLV+DG",
        "use_d1_gate": True,
        "use_reduce50": False,
    },
    {
        "id": "Q1_CLV_D1_M15_REDUCE50",
        "label": "V2_CLV+DG_REDUCE50",
        "use_d1_gate": True,
        "use_reduce50": True,
    },
    {
        "id": "V2_CLV_DG_CONTEXT_EXIT",
        "label": "V2_CLV+DG_CONTEXT_EXIT",
        "use_d1_gate": True,
        "use_reduce50": True,
        "use_context_exit": True,
    },
]

# ── BINANCE API ───────────────────────────────────────

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000
}

async def _fetch_chunk(session, url, params, sem, retries=3):
    async with sem:
        for attempt in range(retries):
            try:
                async with session.get(url, params=params, timeout=10) as res:
                    res.raise_for_status()
                    return await res.json()
            except Exception as e:
                if attempt == retries - 1:
                    print(f"Error fetching binance data chunk: {e}")
                    return []
                await asyncio.sleep(0.5)

async def _fetch_all_chunks(symbol: str, interval: str, start_dt: datetime, end_dt: datetime) -> list:
    url = "https://api.binance.com/api/v3/klines"
    limit = 1000
    
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)
    
    interval_ms = INTERVAL_MS.get(interval, 60_000)
    chunk_span = limit * interval_ms
    
    tasks = []
    sem = asyncio.Semaphore(50)  # Max 50 concurrent requests
    
    async with aiohttp.ClientSession() as session:
        current_start = start_ts
        while current_start < end_ts:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": current_start,
                "endTime": min(current_start + chunk_span - 1, end_ts),
                "limit": limit
            }
            tasks.append(_fetch_chunk(session, url, params, sem))
            current_start += chunk_span
            
        results = await asyncio.gather(*tasks)
    
    candles = []
    for data in results:
        if not data:
            continue
        for row in data:
            dt_utc = datetime.fromtimestamp(row[0]/1000.0, tz=timezone.utc)
            candles.append({
                "time": dt_utc,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4])
            })
            
    # Sort to ensure time sequence
    candles.sort(key=lambda x: x["time"])
    return candles

def fetch_binance_klines(symbol: str, interval: str, start_dt: datetime, end_dt: datetime) -> list:
    """Fetch candles from Binance API concurrently with local CSV caching"""
    clean_sym = symbol.replace("'", "").replace("\\", "")
    if clean_sym == "BTC":
        clean_sym = "BTCUSDT"
        
    os.makedirs("data_cache", exist_ok=True)
    cache_file = f"data_cache/{clean_sym}_{interval}.csv"
    
    cached_df = None
    if os.path.exists(cache_file):
        try:
            cached_df = pd.read_csv(cache_file)
            cached_df["time"] = pd.to_datetime(cached_df["time"], utc=True)
        except Exception as e:
            print(f"Error loading cache: {e}")
            cached_df = None

    ranges_to_fetch = []
    if cached_df is not None and not cached_df.empty:
        cache_min = cached_df["time"].min()
        cache_max = cached_df["time"].max()
        
        if start_dt < cache_min:
            ranges_to_fetch.append((start_dt, min(end_dt, cache_min)))
        if end_dt > cache_max:
            ranges_to_fetch.append((max(start_dt, cache_max), end_dt))
    else:
        ranges_to_fetch.append((start_dt, end_dt))

    if ranges_to_fetch and interval != "1m":
        resampled = []
        for (s, e) in ranges_to_fetch:
            resampled.extend(_resample_from_m1_cache(clean_sym, interval, s, e))

        if resampled:
            new_df = pd.DataFrame(resampled)
            if cached_df is not None and not cached_df.empty:
                combined_df = pd.concat([cached_df, new_df], ignore_index=True)
            else:
                combined_df = new_df

            combined_df["time"] = pd.to_datetime(combined_df["time"], utc=True)
            combined_df.drop_duplicates(subset=["time"], inplace=True)
            combined_df.sort_values("time", inplace=True)
            combined_df.to_csv(cache_file, index=False)
            cached_df = combined_df
            ranges_to_fetch = []
            print(f"Built cache {cache_file} from local M1 data with {len(resampled)} records.")
        
    new_data = []
    for (s, e) in ranges_to_fetch:
        print(f"Fetching missing data for {interval} from {s.strftime('%Y-%m-%d %H:%M')} to {e.strftime('%Y-%m-%d %H:%M')}...")
        chunks = asyncio.run(_fetch_all_chunks(clean_sym, interval, s, e))
        new_data.extend(chunks)
        
    if new_data:
        new_df = pd.DataFrame(new_data)
        if cached_df is not None and not cached_df.empty:
            combined_df = pd.concat([cached_df, new_df], ignore_index=True)
        else:
            combined_df = new_df
            
        combined_df.drop_duplicates(subset=["time"], inplace=True)
        combined_df.sort_values("time", inplace=True)
        
        # Save back to cache
        combined_df.to_csv(cache_file, index=False)
        cached_df = combined_df
        print(f"Updated cache {cache_file} with {len(new_data)} new records.")
    
    if cached_df is not None and not cached_df.empty:
        # Filter to requested range
        mask = (cached_df["time"] >= start_dt) & (cached_df["time"] <= end_dt)
        return cached_df[mask].to_dict('records')
        
    return []

def _resample_from_m1_cache(symbol: str, interval: str, start_dt: datetime, end_dt: datetime) -> list:
    m1_file = f"data_cache/{symbol}_1m.csv"
    if not os.path.exists(m1_file):
        return []

    freq_map = {
        "15m": "15min",
        "1h": "1h",
        "4h": "4h",
        "12h": "12h",
        "1d": "1D",
    }
    freq = freq_map.get(interval)
    if not freq:
        return []

    try:
        df = pd.read_csv(m1_file)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        mask = (df["time"] >= start_dt) & (df["time"] <= end_dt)
        df = df.loc[mask].copy()
        if df.empty:
            return []

        df.set_index("time", inplace=True)
        out = df.resample(freq, label="left", closed="left").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
        }).dropna().reset_index()

        out = out[(out["time"] >= start_dt) & (out["time"] <= end_dt)]
        return out.to_dict("records")
    except Exception as e:
        print(f"Error resampling {interval} from M1 cache: {e}")
        return []


# ── INDICATORS ────────────────────────────────────────
def calc_ema(vals, p):
    n = len(vals)
    out = [None] * n
    if n < p: return out
    k = 2 / (p + 1)
    s = sum(vals[:p]) / p
    out[p-1] = s
    for i in range(p, n):
        s = vals[i] * k + s * (1 - k)
        out[i] = s
    return out

def calc_atr(hi, lo, cl, p):
    n = len(cl)
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i-1]), abs(lo[i] - cl[i-1]))
    out = [None] * n
    if n < p + 1:
        return out
    s = sum(trs[1:p+1]) / p
    out[p] = s
    alpha = 1.0 / p
    for i in range(p+1, n):
        s = (trs[i] - s) * alpha + s
        out[i] = s
    return out

def calc_rsi(vals, p):
    n = len(vals)
    out = [None] * n
    if n <= p: return out
    gains = 0.0
    losses = 0.0
    for i in range(1, p+1):
        change = vals[i] - vals[i-1]
        if change > 0: gains += change
        else: losses -= change
    avg_gain = gains / p
    avg_loss = losses / p
    if avg_loss == 0:
        out[p] = 100.0
    else:
        out[p] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    
    for i in range(p+1, n):
        change = vals[i] - vals[i-1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (p - 1) + gain) / p
        avg_loss = (avg_loss * (p - 1) + loss) / p
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out

# ── BACKTEST ──────────────────────────────────────────
def calc_pnl(side, entry_p, exit_p, size, fee_rate):
    qty     = size / entry_p
    gross   = qty * (exit_p - entry_p) if side == "LONG" else qty * (entry_p - exit_p)
    fee_in  = fee_rate * size
    fee_out = fee_rate * (qty * exit_p)
    net     = gross - fee_in - fee_out
    return net, fee_in + fee_out, gross

def calc_trade_size(entry_p: float, stop_p: float, equity: float) -> float:
    max_notional = max(equity * max(float(LEVERAGE), 1.0), 0.0)
    mode = str(SIZING_MODE).strip().lower()
    if mode.startswith("fixed"):
        desired = max(float(SIZE), float(MIN_SIZE))
        return min(desired, max_notional) if max_notional > 0 else 0.0

    risk = abs(stop_p - entry_p)
    if risk <= 0:
        return 0.0
    qty = (equity * RISK_PER_TRADE_PCT / 100.0) / risk
    desired = max(qty * entry_p, float(MIN_SIZE))
    return min(desired, max_notional) if max_notional > 0 else 0.0

def calc_trade_pnl_from_leg(side: str, entry_p: float, exit_p: float, size: float, fee_rate: float):
    if size <= 0:
        return 0.0, 0.0, 0.0
    return calc_pnl(side, entry_p, exit_p, size, fee_rate)

def _d1_regime_lookup(symbol: str, start: datetime, end: datetime):
    ws = start - timedelta(days=max(WARMUP, 80))
    candles = fetch_binance_klines(symbol, "1d", ws, end)
    if not candles:
        return [], [], [], []

    closes = [c["close"] for c in candles]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    times = [c["time"] for c in candles]
    return candles, ema20, ema50, times

def _passes_d1_downtrend(signal_time: datetime, d1_state: tuple) -> bool:
    d1_candles, d1_ema20, d1_ema50, _ = d1_state
    if not d1_candles:
        return False

    idx = -1
    for k, candle in enumerate(d1_candles):
        d1_close_time = candle["time"] + timedelta(days=1)
        if d1_close_time <= signal_time:
            idx = k
        else:
            break

    if idx < 5 or d1_ema20[idx] is None or d1_ema50[idx] is None or d1_ema20[idx - 5] is None:
        return False

    return (
        d1_candles[idx]["close"] < d1_ema50[idx]
        and d1_ema20[idx] < d1_ema50[idx]
        and (d1_ema20[idx] - d1_ema20[idx - 5]) < 0
    )

def _find_candle_at_or_after(candles: list, start_idx: int, target_time: datetime) -> tuple:
    idx = start_idx
    while idx < len(candles) and candles[idx]["time"] < target_time:
        idx += 1
    if idx >= len(candles):
        return None, idx
    return candles[idx], idx

def _read_context_csv(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "time" not in df.columns:
        return pd.DataFrame()
    try:
        df["time"] = pd.to_datetime(df["time"], utc=True, format="mixed")
    except TypeError:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.sort_values("time").reset_index(drop=True)

def _load_context_data(symbol: str) -> dict:
    return {
        "funding": _read_context_csv(f"data_cache/{symbol}_funding.csv"),
        "oi": _read_context_csv(f"data_cache/{symbol}_oi_coinalyze_daily.csv"),
    }

def _last_row_at_or_before(df: pd.DataFrame, target_time: datetime):
    if df is None or df.empty:
        return None, -1
    idx = df["time"].searchsorted(pd.Timestamp(target_time), side="right") - 1
    if idx < 0:
        return None, -1
    return df.iloc[int(idx)], int(idx)

def _last_completed_daily_row(df: pd.DataFrame, target_time: datetime):
    if df is None or df.empty:
        return None, -1
    close_times = df["time"] + pd.Timedelta(days=1)
    idx = close_times.searchsorted(pd.Timestamp(target_time), side="right") - 1
    if idx < 0:
        return None, -1
    return df.iloc[int(idx)], int(idx)

def _context_exit_decision(signal_time: datetime, context_data: dict) -> tuple:
    funding_row, _ = _last_row_at_or_before(context_data.get("funding"), signal_time)
    oi_row, oi_idx = _last_completed_daily_row(context_data.get("oi"), signal_time)
    oi_prev = None
    oi_df = context_data.get("oi")
    if oi_df is not None and not oi_df.empty and oi_idx > 0:
        oi_prev = oi_df.iloc[oi_idx - 1]

    funding_rate = float(funding_row["funding_rate"]) if funding_row is not None and "funding_rate" in funding_row else None
    oi_close = float(oi_row["oi_close"]) if oi_row is not None and "oi_close" in oi_row else None
    oi_prev_close = float(oi_prev["oi_close"]) if oi_prev is not None and "oi_close" in oi_prev else None
    oi_change_pct = None
    if oi_close is not None and oi_prev_close not in (None, 0):
        oi_change_pct = (oi_close - oi_prev_close) / oi_prev_close * 100.0

    funding_bad = funding_rate is not None and funding_rate <= 0
    oi_bad = oi_close is not None and oi_prev_close is not None and oi_close <= oi_prev_close
    bad_count = int(funding_bad) + int(oi_bad)

    if bad_count >= 2:
        reduce_fraction = 1.0
    elif bad_count == 1:
        reduce_fraction = 0.7
    else:
        reduce_fraction = 0.5

    return reduce_fraction, {
        "funding_at_signal": funding_rate,
        "oi_daily_at_signal": oi_close,
        "oi_daily_prev": oi_prev_close,
        "oi_daily_change_pct": oi_change_pct,
        "context_bad_count": bad_count,
    }

def _simulate_regular_exit(all_m1_candles: list, start_idx: int, et: datetime, sl: float, tp: float):
    n_m1 = len(all_m1_candles)
    for j in range(start_idx, n_m1):
        c = all_m1_candles[j]
        t_curr = c["time"]
        if t_curr < et:
            continue

        hold_time = (t_curr - et).total_seconds() / 3600.0
        if hold_time >= MAX_HOLD_H:
            return t_curr, c["open"], "TIME", j

        if c["high"] >= sl:
            return t_curr, sl, "SL", j
        if c["low"] <= tp:
            return t_curr, tp, "TP", j

    window_end = et + timedelta(hours=MAX_HOLD_H)
    if all_m1_candles and all_m1_candles[-1]["time"] >= window_end:
        for j in range(n_m1 - 1, start_idx - 1, -1):
            c = all_m1_candles[j]
            if et <= c["time"] <= window_end:
                return c["time"], c["close"], "OPEN", j
    if all_m1_candles:
        return all_m1_candles[-1]["time"], all_m1_candles[-1]["close"], "OPEN", n_m1 - 1
    return None, None, "NO_DATA", start_idx

def run_backtest(symbol: str, tf_dict: dict, strategy: dict = None):
    strategy = strategy or STRATEGIES[0]
    print(f"Fetching {tf_dict['label']} candles for {strategy['label']} signal generation...")
    ws = START - timedelta(minutes=tf_dict['mins'] * WARMUP)
    
    signal_candles = fetch_binance_klines(symbol, tf_dict['binance_interval'], ws, END)
    d1_state = _d1_regime_lookup(symbol, START, END) if strategy.get("use_d1_gate") else None
    
    n = len(signal_candles)
    if n < WARMUP:
        print(f"Not enough data for {symbol} {tf_dict['label']}. Needs {WARMUP} bars, got {n}")
        return [], 0
    
    cl = [c["close"] for c in signal_candles]
    hi = [c["high"] for c in signal_candles]
    lo = [c["low"] for c in signal_candles]
    op = [c["open"] for c in signal_candles]
    
    ema_fast = calc_ema(cl, EMA_FAST)
    ema_slow = calc_ema(cl, EMA_SLOW)
    rsi      = calc_rsi(cl, RSI_LEN)
    atr      = calc_atr(hi, lo, cl, ATR_LEN)
    
    signals = []
    filtered = 0
    
    # 1. Identify all valid entry signals on the selected signal timeframe.
    lookback_bars = max(1, int((48 * 60) / tf_dict["mins"])) if strategy.get("use_d1_gate") else 12
    for i in range(lookback_bars, n - 1):
        if ema_fast[i] is None or ema_slow[i] is None or rsi[i] is None or atr[i] is None:
            continue
            
        c1 = ema_fast[i] < ema_slow[i]
        lowest_close = min(cl[i-lookback_bars : i])
        c2 = cl[i] < lowest_close
        c3 = cl[i] < op[i]
        c4 = rsi[i] <= RSI_THRESH
        
        h_l = hi[i] - lo[i]
        clv = (cl[i] - lo[i]) / h_l if h_l != 0 else 0
        c5 = clv <= 0.25
        
        d1_ok = True
        if strategy.get("use_d1_gate"):
            d1_ok = _passes_d1_downtrend(signal_candles[i]["time"] + timedelta(minutes=tf_dict["mins"]), d1_state)
        
        if c1 and c2 and c3 and c4 and c5 and d1_ok:
            et = signal_candles[i+1]["time"]
            if et >= START:
                ep = signal_candles[i+1]["open"]
                sl_price = ep + SL_ATR_MULT * atr[i]
                tp_price = ep - TP_RATIO * (sl_price - ep)
                signals.append({
                    "time": et,
                    "ep": ep,
                    "sl": sl_price,
                    "tp": tp_price,
                    "signal_close": cl[i],
                })
            else:
                filtered += 1
                
    if not signals:
        return [], filtered
        
    # 2. Fetch M1 data for the active trade windows
    print(f"Found {len(signals)} signals. Fetching M1 data to simulate trades...")
    if not signals:
        return [], filtered
        
    first_signal_time = signals[0]["time"]
    m1_end = END + timedelta(hours=MAX_HOLD_H)
    all_m1_candles = fetch_binance_klines(symbol, "1m", first_signal_time, m1_end)
    
    if not all_m1_candles:
        print("Could not fetch M1 candles.")
        return [], filtered
        
    # LƯU M1 DATA THEO YÊU CẦU
    try:
        import os
        import pandas as pd
        os.makedirs("outputs", exist_ok=True)
        pd.DataFrame(all_m1_candles).to_csv(f"outputs/m1_data_{symbol}_{tf_dict['label']}.csv", index=False)
        print(f"Đã lưu {len(all_m1_candles)} nến M1 vào outputs/m1_data_{symbol}_{tf_dict['label']}.csv", flush=True)
    except Exception as e:
        print("Lỗi khi lưu data nến M1:", e)

    m15_candles = []
    if strategy.get("use_reduce50") or DCA_ENABLED:
        m15_end = END + timedelta(hours=MAX_HOLD_H)
        m15_candles = fetch_binance_klines(symbol, "15m", first_signal_time, m15_end)
    context_data = _load_context_data(symbol) if strategy.get("use_context_exit") else {}
        
    trades = []
    cur_eq = CAPITAL
    m1_idx = 0
    n_m1 = len(all_m1_candles)
    
    # 3. Simulate exits on M1 data
    for sig in signals:
        if trades and sig["time"] < trades[-1]["exit_time"]:
            continue
            
        et = sig["time"]
        ep = sig["ep"]
        sl = sig["sl"]
        tp = sig["tp"]
        
        trade_size = calc_trade_size(ep, sl, cur_eq)
        if trade_size <= 0:
            continue
        
        while m1_idx < n_m1 and all_m1_candles[m1_idx]["time"] < et:
            m1_idx += 1
            
        reduced = False
        reduce_time = None
        reduce_price = None
        reduce_net = reduce_fee = reduce_gross = 0.0
        reduce_fraction = 0.0
        dca_added = False
        dca_time = None
        dca_price = None
        dca_size = 0.0
        dca_start_idx = m1_idx
        pre_dca_exit = None
        active_sl = sl
        dca_sl_moved = False
        lev = max(float(LEVERAGE), 1.0)
        first_m15 = None
        context_fields = {
            "funding_at_signal": None,
            "oi_daily_at_signal": None,
            "oi_daily_prev": None,
            "oi_daily_change_pct": None,
            "context_bad_count": 0,
        }

        if m15_candles:
            first_m15, _ = _find_candle_at_or_after(m15_candles, 0, et)

        if DCA_ENABLED and first_m15 and first_m15["close"] < sig["signal_close"]:
            risk_r = max(sl - ep, 0.0)
            dca_trigger_price = ep - DCA_TRIGGER_R * risk_r
            if risk_r > 0 and first_m15["close"] <= dca_trigger_price:
                planned_dca_time = first_m15["time"] + timedelta(minutes=15)
                candidate_exit = _simulate_regular_exit(all_m1_candles, m1_idx, et, sl, tp)
                if candidate_exit[0] and candidate_exit[0] < planned_dca_time:
                    pre_dca_exit = candidate_exit
                else:
                    dca_m1, dca_idx = _find_candle_at_or_after(all_m1_candles, m1_idx, planned_dca_time)
                    if dca_m1:
                        dca_added = True
                        dca_time = dca_m1["time"]
                        dca_price = dca_m1["open"]
                        max_notional = max(cur_eq * lev, 0.0)
                        available_notional = max(max_notional - trade_size, 0.0)
                        dca_size = min(max(trade_size * DCA_SIZE_MULT, 0.0), available_notional)
                        dca_start_idx = dca_idx
                        if dca_size <= 0:
                            dca_added = False
                            dca_time = None
                            dca_price = None
                        if dca_added and DCA_MOVE_SL_TO_ENTRY:
                            active_sl = ep
                            dca_sl_moved = True

        if strategy.get("use_reduce50") and first_m15:
            if first_m15 and first_m15["close"] >= sig["signal_close"]:
                reduce_time = first_m15["time"] + timedelta(minutes=15)
                pre_reduce_exit = _simulate_regular_exit(all_m1_candles, m1_idx, et, sl, tp)
                if pre_reduce_exit[0] and pre_reduce_exit[0] < reduce_time:
                    exit_time, exit_price, result, _ = pre_reduce_exit
                else:
                    reduce_m1, reduce_m1_idx = _find_candle_at_or_after(all_m1_candles, m1_idx, reduce_time)
                    if reduce_m1:
                        reduced = True
                        reduce_time = reduce_m1["time"]
                        reduce_price = reduce_m1["open"]
                        if strategy.get("use_context_exit"):
                            reduce_fraction, context_fields = _context_exit_decision(et, context_data)
                        else:
                            reduce_fraction = 0.5
                        reduce_size = trade_size * reduce_fraction
                        reduce_net, reduce_fee, reduce_gross = calc_pnl("SHORT", ep, reduce_price, reduce_size, FEE_RATE)
                        remaining_size = max(trade_size - reduce_size, 0.0)
                        if remaining_size <= 0:
                            exit_time = reduce_time
                            exit_price = reduce_price
                            result = "CONTEXT100"
                        else:
                            exit_time, exit_price, tail_result, _ = _simulate_regular_exit(
                                all_m1_candles, reduce_m1_idx, et, sl, tp
                            )
                            reduce_label = f"CONTEXT{int(round(reduce_fraction * 100))}" if strategy.get("use_context_exit") else "REDUCE50"
                            result = f"{reduce_label}+{tail_result}"
                    else:
                        exit_time, exit_price, result, _ = _simulate_regular_exit(all_m1_candles, m1_idx, et, sl, tp)
            else:
                if pre_dca_exit:
                    exit_time, exit_price, result, _ = pre_dca_exit
                else:
                    exit_time, exit_price, result, _ = _simulate_regular_exit(all_m1_candles, dca_start_idx, et, active_sl, tp)
                    if dca_added:
                        result = f"DCA+{result}"
        else:
            if pre_dca_exit:
                exit_time, exit_price, result, _ = pre_dca_exit
            else:
                exit_time, exit_price, result, _ = _simulate_regular_exit(all_m1_candles, dca_start_idx, et, active_sl, tp)
                if dca_added:
                    result = f"DCA+{result}"

        final_size = max(trade_size * (1.0 - reduce_fraction), 0.0) if reduced else trade_size
        final_net, final_fee, final_gross = calc_pnl("SHORT", ep, exit_price, final_size, FEE_RATE)
        dca_net, dca_fee, dca_gross = calc_trade_pnl_from_leg("SHORT", dca_price, exit_price, dca_size, FEE_RATE) if dca_added else (0.0, 0.0, 0.0)
        net = reduce_net + final_net + dca_net
        fee = reduce_fee + final_fee + dca_fee
        gross = reduce_gross + final_gross + dca_gross
        equity_at_entry = cur_eq
        cur_eq += net
        trades.append({
            "side": "SHORT", "entry": ep, "exit": exit_price,
            "result": result, "net": net, "fee": fee, "gross": gross,
            "dca_net": dca_net, "dca_fee": dca_fee, "dca_gross": dca_gross,
            "net_nofee": gross, "size": trade_size + dca_size,
            "entry_time": et, "exit_time": exit_time,
            "sl": sl, "tp": tp,
            "strategy": strategy["label"],
            "reduced": reduced,
            "reduce_fraction": reduce_fraction,
            "reduce_time": reduce_time,
            "reduce_price": reduce_price,
            "dca_added": dca_added,
            "dca_time": dca_time,
            "dca_price": dca_price,
            "dca_size": dca_size,
            "dca_sl_moved": dca_sl_moved,
            "active_sl": active_sl,
            "leverage": lev,
            "equity_at_entry": equity_at_entry,
            "margin_required": (trade_size + dca_size) / lev if lev > 0 else 0.0,
            "margin_usage_pct": ((trade_size + dca_size) / lev / equity_at_entry * 100.0) if equity_at_entry > 0 and lev > 0 else 0.0,
            **context_fields,
        })

    trades = [t for t in trades if START <= t["entry_time"] < END]
    return trades, filtered

def compute_stats(trades):
    if not trades:
        return None

    wins       = [t for t in trades if t["net"] > 0]
    losses     = [t for t in trades if t["net"] <= 0]
    wins_nofee = [t for t in trades if t["net_nofee"] > 0]
    longs      = [t for t in trades if t["side"] == "LONG"]
    shorts     = [t for t in trades if t["side"] == "SHORT"]

    total_net   = sum(t["net"]   for t in trades)
    total_fee   = sum(t["fee"]   for t in trades)
    total_gross = sum(t["gross"] for t in trades)
    turnover    = total_fee / FEE_RATE if FEE_RATE > 0 else sum(t["size"] * 2 for t in trades)
    wr          = len(wins)       / len(trades) * 100 if trades else 0
    wr_nofee    = len(wins_nofee) / len(trades) * 100 if trades else 0

    gw = sum(t["net"] for t in wins)   if wins   else 0
    gl = abs(sum(t["net"] for t in losses)) if losses else 0.001
    pf = gw / gl
    avg_trade = total_net / len(trades)
    avg_win = gw / len(wins) if wins else 0
    avg_loss = sum(t["net"] for t in losses) / len(losses) if losses else 0
    payoff_ratio = avg_win / abs(avg_loss) if avg_loss < 0 else 0
    expectancy = avg_trade
    fee_to_gross_profit_pct = total_fee / gw * 100 if gw > 0 else 0
    net_gross_ratio = total_net / total_gross if total_gross > 0 else 0
    hold_hours = [(t["exit_time"] - t["entry_time"]).total_seconds() / 3600.0 for t in trades]
    avg_hold_h = sum(hold_hours) / len(hold_hours) if hold_hours else 0
    sorted_hold = sorted(hold_hours)
    if sorted_hold:
        mid = len(sorted_hold) // 2
        median_hold_h = sorted_hold[mid] if len(sorted_hold) % 2 else (sorted_hold[mid - 1] + sorted_hold[mid]) / 2
    else:
        median_hold_h = 0
    exposure_h = sum(hold_hours)
    total_period_h = max((END - START).total_seconds() / 3600.0, 0.001)
    exposure_pct = min(exposure_h / total_period_h * 100, 100)
    trades_per_month = len(trades) / max(((END - START).days / 30.4375), 0.001)
    max_margin_required = max((t.get("margin_required", 0.0) for t in trades), default=0.0)
    avg_margin_required = sum(t.get("margin_required", 0.0) for t in trades) / len(trades) if trades else 0.0
    max_margin_usage_pct = max((t.get("margin_usage_pct", 0.0) for t in trades), default=0.0)

    equity = CAPITAL; peak = CAPITAL; max_dd = 0; max_dd_pct = 0
    eq_curve = [CAPITAL]
    eq_times = [trades[0]["entry_time"].strftime("%Y-%m-%d")] if trades else []
    dd_curve = [0.0]
    for t in trades:
        equity += t["net"]
        eq_curve.append(equity)
        eq_times.append(t["exit_time"].strftime("%Y-%m-%d"))
        peak = max(peak, equity)
        dd   = peak - equity
        dd_curve.append(-dd)
        if dd > max_dd:
            max_dd     = dd
            max_dd_pct = dd / peak * 100

    returns = [t["net"] / t["size"] for t in trades if t["size"] > 0]
    if len(returns) > 1:
        avg_r = sum(returns) / len(returns)
        var_r = sum((r - avg_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(var_r) if var_r > 0 else 0.001
        trades_per_year = len(trades) / max(((END - START).days / 365.25), 0.001)
        sharpe = (avg_r / std_r) * math.sqrt(trades_per_year)
    else:
        sharpe = 0

    l_wins     = sum(1 for t in longs  if t["net"] > 0)
    s_wins     = sum(1 for t in shorts if t["net"] > 0)
    l_wr       = l_wins / max(len(longs),  1) * 100
    s_wr       = s_wins / max(len(shorts), 1) * 100
    l_net      = sum(t["net"] for t in longs)
    s_net      = sum(t["net"] for t in shorts)
    l_wr_nofee = sum(1 for t in longs  if t["net_nofee"] > 0) / max(len(longs),  1) * 100
    s_wr_nofee = sum(1 for t in shorts if t["net_nofee"] > 0) / max(len(shorts), 1) * 100

    tp_n  = sum(1 for t in trades if "TP" in t["result"])
    sl_n  = sum(1 for t in trades if "SL" in t["result"])
    time_n = sum(1 for t in trades if "TIME" in t["result"])
    open_n = sum(1 for t in trades if "OPEN" in t["result"])
    no_data_n = sum(1 for t in trades if "NO_DATA" in t["result"])
    dca_n = sum(1 for t in trades if t.get("dca_added"))
    dca_net = sum(t.get("dca_net", 0.0) for t in trades)
    dca_fee = sum(t.get("dca_fee", 0.0) for t in trades)
    dca_gross = sum(t.get("dca_gross", 0.0) for t in trades)
    dca_win_n = sum(1 for t in trades if t.get("dca_added") and t.get("dca_net", 0.0) > 0)
    dca_loss_n = sum(1 for t in trades if t.get("dca_added") and t.get("dca_net", 0.0) <= 0)
    dca_sl_moved_n = sum(1 for t in trades if t.get("dca_sl_moved"))
    dca_rate = dca_n / len(trades) * 100 if trades else 0

    max_win_streak = max_lose_streak = cur_w = cur_l = 0
    for t in trades:
        if t["net"] > 0:
            cur_w += 1; cur_l = 0
            max_win_streak = max(max_win_streak, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_lose_streak = max(max_lose_streak, cur_l)

    months = {}
    for t in trades:
        m = t["entry_time"].strftime("%Y-%m")
        if m not in months:
            months[m] = {"net": 0, "cnt": 0, "wins": 0, "fee": 0}
        months[m]["net"] += t["net"]
        months[m]["cnt"] += 1
        months[m]["fee"] += t["fee"]
        if t["net"] > 0:
            months[m]["wins"] += 1

    return {
        "trades": len(trades), "wr": wr, "wr_nofee": wr_nofee,
        "wins": len(wins), "losses": len(losses),
        "net": total_net, "fee": total_fee, "gross": total_gross,
        "final": CAPITAL + total_net, "pf": pf, "turnover": turnover,
        "avg_trade": avg_trade, "avg_win": avg_win, "avg_loss": avg_loss,
        "payoff_ratio": payoff_ratio, "expectancy": expectancy,
        "fee_to_gross_profit_pct": fee_to_gross_profit_pct,
        "net_gross_ratio": net_gross_ratio,
        "avg_hold_h": avg_hold_h, "median_hold_h": median_hold_h,
        "exposure_pct": exposure_pct, "trades_per_month": trades_per_month,
        "leverage": LEVERAGE, "max_margin_required": max_margin_required,
        "avg_margin_required": avg_margin_required, "max_margin_usage_pct": max_margin_usage_pct,
        "dd": max_dd, "dd_pct": max_dd_pct, "sharpe": sharpe,
        "longs": len(longs), "l_wr": l_wr, "l_net": l_net, "l_wr_nofee": l_wr_nofee,
        "shorts": len(shorts), "s_wr": s_wr, "s_net": s_net, "s_wr_nofee": s_wr_nofee,
        "tp": tp_n, "sl": sl_n, "time": time_n, "open": open_n, "no_data": no_data_n,
        "dca_count": dca_n, "dca_rate": dca_rate,
        "dca_net": dca_net, "dca_fee": dca_fee, "dca_gross": dca_gross,
        "dca_wins": dca_win_n, "dca_losses": dca_loss_n,
        "dca_sl_moved_count": dca_sl_moved_n,
        "cut": 0, "rev": 0,
        "max_win_streak": max_win_streak, "max_lose_streak": max_lose_streak,
        "eq_curve": eq_curve, "eq_times": eq_times, "dd_curve": dd_curve,
        "months": months,
    }

def run_coin(sym: str, tf: dict, strategy: dict = None):
    strategy = strategy or STRATEGIES[0]
    t0 = time.time()
    try:
        t1 = time.time()
        trades, filtered = run_backtest(sym, tf, strategy)
        t_bt = time.time() - t1
        t_fetch = t_bt
    except Exception as e:
        return {"label": sym.replace("USDT", ""), "tf": tf["label"], "strategy": strategy["label"], "error": str(e)[:120]}

    secs  = time.time() - t0
    label = sym.replace("USDT", "")

    if not trades:
        return {"label": label, "tf": tf["label"], "trades": 0,
                "strategy": strategy["label"], "strategy_id": strategy["id"],
                "secs": secs, "fetch_secs": t_fetch, "bt_secs": t_bt}

    stats = compute_stats(trades)
    stats["label"]      = label
    stats["tf"]         = tf["label"]
    stats["strategy"]   = strategy["label"]
    stats["strategy_id"] = strategy["id"]
    stats["secs"]       = secs
    stats["fetch_secs"] = t_fetch
    stats["bt_secs"]    = t_bt
    stats["filtered"]   = filtered
    stats["trade_rows"] = [
        {
            "symbol":     label,
            "strategy":   strategy["label"],
            "strategy_id": strategy["id"],
            "timeframe":  tf["label"],
            "side":       t["side"],
            "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M"),
            "exit_time":  t["exit_time"].strftime("%Y-%m-%d %H:%M"),
            "entry":      round(t["entry"],  6),
            "exit":       round(t["exit"],   6),
            "size":       round(t["size"],   2),
            "gross":      round(t["gross"],  4),
            "fee":        round(t["fee"],    4),
            "net":        round(t["net"],    4),
            "result":     t["result"],
            "sl":         round(t["sl"],     6),
            "tp":         round(t["tp"],     6),
            "reduced":    t.get("reduced", False),
            "reduce_fraction": round(t.get("reduce_fraction", 0.0), 2),
            "reduce_time": t["reduce_time"].strftime("%Y-%m-%d %H:%M") if t.get("reduce_time") else "",
            "reduce_price": round(t["reduce_price"], 6) if t.get("reduce_price") else "",
            "dca_added": t.get("dca_added", False),
            "dca_time": t["dca_time"].strftime("%Y-%m-%d %H:%M") if t.get("dca_time") else "",
            "dca_price": round(t["dca_price"], 6) if t.get("dca_price") else "",
            "dca_size": round(t.get("dca_size", 0.0), 2),
            "dca_sl_moved": t.get("dca_sl_moved", False),
            "active_sl": round(t.get("active_sl", t["sl"]), 6),
            "dca_net": round(t.get("dca_net", 0.0), 4),
            "dca_fee": round(t.get("dca_fee", 0.0), 4),
            "leverage": t.get("leverage", LEVERAGE),
            "margin_required": round(t.get("margin_required", 0.0), 4),
            "margin_usage_pct": round(t.get("margin_usage_pct", 0.0), 4),
            "funding_at_signal": round(t["funding_at_signal"], 8) if t.get("funding_at_signal") is not None else "",
            "oi_daily_at_signal": round(t["oi_daily_at_signal"], 4) if t.get("oi_daily_at_signal") is not None else "",
            "oi_daily_prev": round(t["oi_daily_prev"], 4) if t.get("oi_daily_prev") is not None else "",
            "oi_daily_change_pct": round(t["oi_daily_change_pct"], 4) if t.get("oi_daily_change_pct") is not None else "",
            "context_bad_count": t.get("context_bad_count", 0),
        }
        for t in trades
    ]
    return stats

def fetch_chart_candles(symbol: str, tf: dict) -> list:
    # Fetch chart candles
    # the frontend expects unix timestamps for the 'time' field
    ws = START - timedelta(minutes=tf['mins'] * WARMUP)
    candles = fetch_binance_klines(symbol, tf["binance_interval"], ws, END)
    for c in candles:
        c["time"] = int(c["time"].timestamp())
    return candles

if __name__ == "__main__":
    print("=" * 60)
    print(" ALPHA-1 BACKTEST V2 (TradingView)")
    print(f" Period: {START.strftime('%Y-%m-%d %H:%M')} -> {END.strftime('%Y-%m-%d %H:%M')} (UTC)")
    print("=" * 60)
    
    symbol = "BTCUSDT"
    for tf in TIMEFRAMES:
        trades, filtered = run_backtest(symbol, tf)
        stats = compute_stats(trades)
        
        print(f"\n--- Results for {tf['label']} ---")
        if stats:
            print(f" Trades: {stats['trades']}")
            print(f" Win Rate: {stats['wr']:.2f}%")
            print(f" Net PnL: ${stats['net']:.2f}")
            for t in trades:
                print(f"  > {t['entry_time'].strftime('%m-%d %H:%M')} | {t['result']:4s} | PnL: ${t['net']:>7.2f} | Entry: {t['entry']:.1f}")
        else:
            print(" No trades executed in this period.")
