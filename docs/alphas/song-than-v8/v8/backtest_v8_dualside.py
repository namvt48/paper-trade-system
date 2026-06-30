"""
Song Than V8 — DUAL SIDE từ một sự kiện phá Green Zone.

Insight (heavy-think 3 agent hội tụ): LONG không mirror SHORT. Cùng một cú phá
xuống Green Zone sinh ra HAI nhánh loại trừ lẫn nhau:

  Nến b1 đóng DƯỚI mép ngoài Green Zone (green_low) = phá thật.
  ├─ RECLAIM ≤3 nến (đóng lại > mép*(1+m)) = BẪY GẤU  -> LONG (cover cascade)
  └─ KHÔNG reclaim đến nến 4 (cả 4 đóng dưới mép)     -> SHORT (continuation, V7)

LONG (bẫy gấu): vào market tại close nến reclaim. SL dưới đáy bẫy (clamp 1.75-2.5%),
TP = SL x1.43, hold <=32. SHORT giữ nguyên V7.

So sánh 3 cấu hình: SHORT-only (V7) | LONG-only (reclaim) | DUAL (cả hai).

Chạy:  python3 backtest_v8_dualside.py
"""

import math
import os
import numpy as np
import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
SWING_LENGTH = 50
WAIT = 4
RECLAIM_N = 3          # số nến cho phép reclaim
RECLAIM_M = 0.0015     # buffer reclaim 15bps (vượt nhiễu wick-retest)
HOLD_SHORT = 64
HOLD_LONG = 32
SL_CLAMP = (0.0175, 0.025)
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


def gen_signals(sym, sl_pct):
    """Sinh cả LONG (reclaim) lẫn SHORT (continuation) từ các cú phá Green Zone."""
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

    longs, shorts = [], []
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
                if close[j] < outer:  # phá thật tại nến j = b1
                    b1 = j
                    end = j
                    # nhánh LONG: reclaim trong RECLAIM_N nến
                    rec = None
                    for k in range(1, RECLAIM_N + 1):
                        if b1 + k >= N:
                            break
                        if close[b1 + k] > outer * (1 + RECLAIM_M):
                            rec = b1 + k
                            break
                    if rec is not None:
                        entry = close[rec]
                        trap_low = low[b1:rec + 1].min()
                        slp = min(max(entry / trap_low - 1, SL_CLAMP[0]), SL_CLAMP[1])
                        longs.append(_sim(high, low, close, times, rec, entry, True, slp, HOLD_LONG, N))
                        end = rec
                    # nhánh SHORT: không reclaim đến nến WAIT
                    elif b1 + WAIT < N and all(close[b1 + k] < outer for k in range(1, WAIT + 1)):
                        e = b1 + WAIT
                        shorts.append(_sim(high, low, close, times, e, close[e], False, sl_pct, HOLD_SHORT, N))
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
    return longs, shorts


def _sim(high, low, close, times, e, entry, is_long, sl_pct, hold, N):
    sgn = 1 if is_long else -1
    sl_p = entry * (1 - sgn * sl_pct)
    tp_p = entry * (1 + sgn * sl_pct * TP_RATIO)
    end = min(e + hold, N - 1)
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
            "t_in": times[e], "t_out": times[end], "pct": pct - 2 * TAKER_FEE, "reason": reason}


def run_portfolio(cands, t0, t1):
    cands = sorted([c for c in cands if t0 <= c["t_in"] < t1], key=lambda c: c["t_in"])
    balance = INITIAL_BALANCE
    open_pos = {}
    trades = []
    for c in cands:
        open_pos = {k: t for k, t in open_pos.items() if t > c["t_in"]}
        key = c["sym"]
        if key in open_pos or len(open_pos) >= MAX_OPEN:
            continue
        notional = balance * RISK_PCT / c["sl"]
        balance += notional * c["pct"]
        trades.append({**c, "pnl": notional * c["pct"], "balance": balance})
        open_pos[key] = c["t_out"]
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
    return (f"  vốn {final:>9,.0f} ({(final/INITIAL_BALANCE-1)*100:+6.1f}%) | LN/th {monthly:+5.2f}% | "
            f"DD {dd:6.1f}% | WR {(tr.pnl>0).mean()*100:4.1f}% | PF {pf:.2f} | {len(tr)} lệnh")


def main():
    print("V8 DUAL-SIDE — LONG bẫy gấu (reclaim) + SHORT continuation, từ cùng cú phá Green Zone\n")
    all_long, all_short = [], []
    for sym, sl in COINS.items():
        lo, sh = gen_signals(sym, sl)
        for c in lo:
            c["sym"] = sym
        for c in sh:
            c["sym"] = sym
        all_long += lo
        all_short += sh
        print(f"  {sym:<10}: {len(lo):>4} LONG | {len(sh):>4} SHORT")

    for name, t0, t1 in [("IS 2020-2024", 0, CUT_OOS), ("OOS 2025-nay", CUT_OOS, 4_000_000_000_000)]:
        print(f"\n===== {name} =====")
        for label, pool in [("SHORT-only (V7)", all_short),
                            ("LONG-only (bẫy gấu)", all_long),
                            ("DUAL (long+short)", all_long + all_short)]:
            tr, final = run_portfolio(pool, t0, t1)
            ts = tr["t_in"] if not tr.empty else pd.Series([t0, t1])
            print(f"  {label:<22}{stats(tr, final, ts.min(), ts.max())}")
            if label == "DUAL (long+short)" and not tr.empty:
                for s, g in tr.groupby("side"):
                    print(f"      {s}: {len(g)} lệnh | PnL {g.pnl.sum():>+10,.2f} | win {(g.pnl>0).mean()*100:.1f}%")


if __name__ == "__main__":
    main()
