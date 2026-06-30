"""
Song Than V9 — DUAL SIDE bằng REGIME GATING (heavy-think: long-regime agent).

LONG không mirror SHORT mà gate theo macro regime:
  Regime tính trên daily resample mỗi coin:
    BULL = close_d>EMA200 & EMA50>EMA200 & ret60>0 & slope(EMA200,20)>0
    STRONG_BULL = BULL & ret60>0.40
    BEAR = close_d<EMA200 & EMA50<EMA200 & ret60<0
    SIDEWAYS = còn lại

  LONG (pullback-to-support, CHỈ trong BULL/STRONG_BULL):
    giá chạm Green Zone (low<=green_high) rồi ĐÓNG lại >= mép trong WAIT nến
    mà KHÔNG nến nào đóng dưới green_low (hỗ trợ giữ) -> LONG market.
    SL/TP/hold = như SHORT (sl coin, x1.43, hold 64).

  SHORT (V7 continuation) throttle theo regime:
    BEAR x1.25 | SIDEWAYS x1.0 | BULL x0.5 (TP x1.25, hold 32) | STRONG_BULL OFF

So sánh ablation: V7 short-only | +throttle short | +long pullback (= V9 full).

Chạy:  python3 backtest_v9_regime.py
"""

import math
import os
import numpy as np
import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
SWING_LENGTH = 50
WAIT = 4
HOLD = 64
TP_RATIO = 1.43
RISK_PCT = 0.005
MAX_OPEN = 6
TAKER_FEE = 0.0005
INITIAL_BALANCE = 10_000.0
CUT_OOS = 1735689600000
EPISODE_CAP = 200

COINS = {
    "BTCUSDT": 0.0175, "ETHUSDT": 0.025, "APTUSDT": 0.025, "SANDUSDT": 0.025,
    "AVAXUSDT": 0.025, "XRPUSDT": 0.025, "ENSUSDT": 0.025, "MAGICUSDT": 0.025,
    "CFXUSDT": 0.025, "DOGEUSDT": 0.025,
}


def compute_swing_points(high, low, L=SWING_LENGTH):
    N = len(high)
    sh, sl_ = np.full(N, np.nan), np.full(N, np.nan)
    leg = 0
    for i in range(L, N):
        p = i - L
        if high[p] > high[p + 1: i + 1].max():
            nl = 0
        elif low[p] < low[p + 1: i + 1].min():
            nl = 1
        else:
            continue
        if nl != leg:
            (sh if nl == 0 else sl_)[p] = high[p] if nl == 0 else low[p]
            leg = nl
    return sh, sl_


def compute_trailing(sh, sl_, high, low, L=SWING_LENGTH):
    N = len(high)
    t_up, t_dn = np.empty(N), np.empty(N)
    leg, cu, cd = 0, -math.inf, math.inf
    for i in range(N):
        if i >= L:
            p = i - L
            if not np.isnan(sh[p]) and leg != 0:
                cu, leg = sh[p], 0
            if not np.isnan(sl_[p]) and leg != 1:
                cd, leg = sl_[p], 1
        cu, cd = max(cu, high[i]), min(cd, low[i])
        t_up[i], t_dn[i] = cu, cd
    return t_up, t_dn


def load(sym):
    return pd.read_csv(f"{DIR}/{sym}_15m_full.csv")[["open_time", "open", "high", "low", "close"]] \
        .sort_values("open_time").reset_index(drop=True)


def compute_regime(df):
    """Trả về mảng regime theo từng nến 15m (ffill từ daily)."""
    d = df.copy()
    d["day"] = (d["open_time"] // 86_400_000)
    daily = d.groupby("day")["close"].last()
    e200 = daily.ewm(span=200, adjust=False).mean()
    e50 = daily.ewm(span=50, adjust=False).mean()
    ret60 = daily / daily.shift(60) - 1
    slope = e200 / e200.shift(20) - 1
    reg = pd.Series("SIDEWAYS", index=daily.index)
    bull = (daily > e200) & (e50 > e200) & (ret60 > 0) & (slope > 0)
    bear = (daily < e200) & (e50 < e200) & (ret60 < 0)
    reg[bear] = "BEAR"
    reg[bull] = "BULL"
    reg[bull & (ret60 > 0.40)] = "STRONG_BULL"
    return d["day"].map(reg).to_numpy()


def gen_signals(sym, sl_pct):
    df = load(sym)
    high, low, close = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    times = df["open_time"].to_numpy()
    reg = compute_regime(df)
    N = len(df)
    sh, sl_ = compute_swing_points(high, low)
    t_up, t_dn = compute_trailing(sh, sl_, high, low)
    g_low = t_dn
    g_high = t_dn * 0.95 + t_up * 0.05
    r_low = t_up * 0.95 + t_dn * 0.05
    valid = t_up > t_dn

    sigs = []
    i = SWING_LENGTH * 2
    while i < N - 1:
        if not valid[i - 1]:
            i += 1
            continue
        touch_dn = low[i] <= g_high[i - 1]
        touch_up = high[i] >= r_low[i - 1]
        if touch_dn == touch_up:
            i += 1
            continue
        end = None
        if touch_dn:
            outer, inner = g_low[i - 1], g_high[i - 1]
            broke = False
            for j in range(i, min(i + EPISODE_CAP, N)):
                if close[j] < outer:  # phá thật -> SHORT continuation (V7)
                    broke = True
                    b1 = j
                    end = j
                    if b1 + WAIT < N and all(close[b1 + k] < outer for k in range(1, WAIT + 1)):
                        e = b1 + WAIT
                        sigs.append(_sim(high, low, close, times, e, False, sl_pct, HOLD, N, reg[e]))
                        end = e
                    break
                if close[j] > inner:  # hỗ trợ giữ (đóng lại trên mép trong) -> LONG nếu BULL
                    end = j
                    if reg[j] in ("BULL", "STRONG_BULL"):
                        sigs.append(_sim(high, low, close, times, j, True, sl_pct, HOLD, N, reg[j]))
                    break
        else:
            for j in range(i, min(i + EPISODE_CAP, N)):
                if close[j] > t_up[i - 1] or close[j] < r_low[i - 1]:
                    end = j
                    break
        i = (end + 1) if end is not None else min(i + EPISODE_CAP, N)
    return sigs


def _sim(high, low, close, times, e, is_long, sl_pct, hold, N, regime):
    sgn = 1 if is_long else -1
    # throttle SHORT theo regime
    size, tp_mult, h = 1.0, TP_RATIO, hold
    if not is_long:
        if regime == "STRONG_BULL":
            return None
        if regime == "BULL":
            size, tp_mult, h = 0.5, 1.25, 32
        elif regime == "BEAR":
            size = 1.25
    entry = close[e]
    sl_p = entry * (1 - sgn * sl_pct)
    tp_p = entry * (1 + sgn * sl_pct * tp_mult)
    end = min(e + h, N - 1)
    pct, reason = None, "TIME"
    for j in range(e + 1, end + 1):
        hit_sl = low[j] <= sl_p if is_long else high[j] >= sl_p
        hit_tp = high[j] >= tp_p if is_long else low[j] <= tp_p
        if hit_sl:
            pct, end, reason = sgn * (sl_p - entry) / entry, j, "SL"
            break
        if hit_tp:
            pct, end, reason = sgn * (tp_p - entry) / entry, j, "TP"
            break
    if pct is None:
        pct = sgn * (close[end] - entry) / entry
    return {"sym": None, "sl": sl_pct, "side": "LONG" if is_long else "SHORT",
            "t_in": times[e], "t_out": times[end], "pct": pct - 2 * TAKER_FEE,
            "reason": reason, "size": size}


def run_portfolio(cands, t0, t1):
    cands = sorted([c for c in cands if c and t0 <= c["t_in"] < t1], key=lambda c: c["t_in"])
    balance = INITIAL_BALANCE
    open_pos, trades = {}, []
    for c in cands:
        open_pos = {k: t for k, t in open_pos.items() if t > c["t_in"]}
        if c["sym"] in open_pos or len(open_pos) >= MAX_OPEN:
            continue
        notional = balance * RISK_PCT * c["size"] / c["sl"]
        balance += notional * c["pct"]
        trades.append({**c, "pnl": notional * c["pct"], "balance": balance})
        open_pos[c["sym"]] = c["t_out"]
    return pd.DataFrame(trades), balance


def stats(tr, final, t0, t1):
    if tr.empty:
        return "  (không lệnh)"
    days = (t1 - t0) / 86_400_000
    monthly = ((final / INITIAL_BALANCE) ** (30.44 / days) - 1) * 100 if final > 0 else float("nan")
    eq = tr["balance"]
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    pfd = abs(tr.loc[tr.pnl <= 0, "pnl"].sum())
    pf = tr.loc[tr.pnl > 0, "pnl"].sum() / pfd if pfd else float("inf")
    return (f"vốn {final:>9,.0f} ({(final/INITIAL_BALANCE-1)*100:+6.1f}%) | LN/th {monthly:+5.2f}% | "
            f"DD {dd:6.1f}% | WR {(tr.pnl>0).mean()*100:4.1f}% | PF {pf:.2f} | {len(tr)} lệnh")


def main():
    print("V9 REGIME-GATED DUAL — long pullback (chỉ BULL) + short throttle theo regime\n")
    base_short, full = [], []  # base_short = short không throttle; full = throttle+long
    for sym, sl in COINS.items():
        sigs = [s for s in gen_signals(sym, sl) if s]
        for c in sigs:
            c["sym"] = sym
        full += sigs
        nl = sum(1 for s in sigs if s["side"] == "LONG")
        print(f"  {sym:<10}: {nl:>4} LONG (bull) | {len(sigs)-nl:>4} SHORT")

    # tái tạo short-only V7 (không throttle): chạy lại với size=1, không gate
    short_v7 = []
    for sym, sl in COINS.items():
        for s in gen_signals_v7(sym, sl):
            s["sym"] = sym
            short_v7.append(s)

    for name, t0, t1 in [("IS 2020-2024", 0, CUT_OOS), ("OOS 2025-nay", CUT_OOS, 4_000_000_000_000)]:
        print(f"\n===== {name} =====")
        for label, pool in [("SHORT-only V7 (base)", short_v7),
                            ("+throttle +long (V9)", full)]:
            tr, final = run_portfolio(pool, t0, t1)
            ts = tr["t_in"] if not tr.empty else pd.Series([t0, t1])
            print(f"  {label:<22} {stats(tr, final, ts.min(), ts.max())}")
            if label.startswith("+throttle") and not tr.empty:
                for s, g in tr.groupby("side"):
                    print(f"      {s}: {len(g)} lệnh | PnL {g.pnl.sum():>+10,.2f} | win {(g.pnl>0).mean()*100:.1f}%")


def gen_signals_v7(sym, sl_pct):
    """SHORT-only V7 không throttle (size=1, hold 64, mọi regime)."""
    df = load(sym)
    high, low, close = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    times = df["open_time"].to_numpy()
    N = len(df)
    sh, sl_ = compute_swing_points(high, low)
    t_up, t_dn = compute_trailing(sh, sl_, high, low)
    g_low = t_dn
    g_high = t_dn * 0.95 + t_up * 0.05
    r_low = t_up * 0.95 + t_dn * 0.05
    valid = t_up > t_dn
    out = []
    i = SWING_LENGTH * 2
    while i < N - 1:
        if not valid[i - 1]:
            i += 1
            continue
        touch_dn = low[i] <= g_high[i - 1]
        touch_up = high[i] >= r_low[i - 1]
        if touch_dn == touch_up:
            i += 1
            continue
        end = None
        if touch_dn:
            outer = g_low[i - 1]
            for j in range(i, min(i + EPISODE_CAP, N)):
                if close[j] < outer:
                    b1 = j
                    end = j
                    if b1 + WAIT < N and all(close[b1 + k] < outer for k in range(1, WAIT + 1)):
                        e = b1 + WAIT
                        s = _sim(high, low, close, times, e, False, sl_pct, HOLD, N, "SIDEWAYS")
                        out.append(s)
                        end = e
                    break
                if close[j] > g_high[i - 1]:
                    end = j
                    break
        else:
            for j in range(i, min(i + EPISODE_CAP, N)):
                if close[j] > t_up[i - 1] or close[j] < r_low[i - 1]:
                    end = j
                    break
        i = (end + 1) if end is not None else min(i + EPISODE_CAP, N)
    return out


if __name__ == "__main__":
    main()
