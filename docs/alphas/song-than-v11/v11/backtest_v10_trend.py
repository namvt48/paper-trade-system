"""
Song Than V10 — LONG trend-following trên 4H (heavy-think: long-trend2 agent).

KHÔNG dùng zone Song Than. Khai thác trend-persistence (time-series momentum)
trên khung 4h — anomaly độc lập, sống OOS nơi level-bounce 15m chết.

RULE 1 (Donchian breakout): close 4h tạo đỉnh 55-bar mới + EMA50>EMA200 +
  30d-return>0 -> LONG next open. Chandelier trail HH-3*ATR, time-stop 10 bar nếu chưa lời.
RULE 2 (EMA pullback): uptrend (EMA50>EMA200), giá về band EMA20-50 rồi đóng lại
  trên EMA20 (nến xanh), RSI không thủng 40 -> LONG. SL chặt (min swing-low / 2ATR),
  chốt 1/2 tại +2R, trail phần còn lại.

Tham số khoá ở giá trị CTA chuẩn (không tối ưu per-coin -> chống overfit).
So sánh: LONG trend trên 10 coin, IS/OOS, kèm tham chiếu SHORT V7.

Chạy:  python3 backtest_v10_trend.py
"""

import os
import numpy as np
import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))
CUT_OOS = 1735689600000
INITIAL_BALANCE = 10_000.0
RISK_PCT = 0.005       # rủi ro 0.5% equity / lệnh (qua khoảng cách SL)
TAKER_FEE = 0.0005
MAX_OPEN = 6
COINS = ["BTCUSDT", "ETHUSDT", "APTUSDT", "SANDUSDT", "AVAXUSDT",
         "XRPUSDT", "ENSUSDT", "MAGICUSDT", "CFXUSDT", "DOGEUSDT"]


def load_4h(sym):
    df = pd.read_csv(f"{DIR}/{sym}_15m_full.csv")
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms")
    h4 = df.set_index("dt").resample("4h").agg(
        open_time=("open_time", "first"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last")).dropna().reset_index(drop=True)
    d1 = df.set_index("dt").resample("1D").agg(close=("close", "last")).dropna()
    return h4, d1


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(h, l, c, n=14):
    tr = np.maximum(h - l, np.maximum((h - c.shift()).abs(), (l - c.shift()).abs()))
    return tr.rolling(n).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, 1e-9))


def signals(sym):
    h4, d1 = load_4h(sym)
    c = h4["close"]
    h, l = h4["high"], h4["low"]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    A = atr(h, l, c)
    R = rsi(c)
    don55 = c.rolling(55).max()
    # regime: 30d return > 0 & close > EMA200 daily  (map về 4h theo ngày)
    d_e200 = ema(d1["close"], 200)
    ret30 = d1["close"] / d1["close"].shift(30) - 1
    bull_d = ((d1["close"] > d_e200) & (ret30 > 0))
    day_key = pd.to_datetime(h4["open_time"], unit="ms").dt.floor("D")
    bull = day_key.map(bull_d).fillna(False).to_numpy()

    cN = c.to_numpy(); hN = h.to_numpy(); lN = l.to_numpy(); oN = h4["open"].to_numpy()
    e20N, e50N, e200N = e20.to_numpy(), e50.to_numpy(), e200.to_numpy()
    AN, RN, donN = A.to_numpy(), R.to_numpy(), don55.to_numpy()
    times = h4["open_time"].to_numpy()
    N = len(h4)

    trades = []

    def simulate(e, entry, sl0, rule):
        """Trail chandelier HH-3ATR (rule1) hoặc scale+trail (rule2)."""
        sl = sl0
        hh = entry
        half_done = False
        realized = 0.0
        rem = 1.0
        r_dist = entry - sl0
        tp2 = entry + 2 * r_dist
        for j in range(e + 1, min(e + 120, N)):
            hh = max(hh, hN[j])
            # chốt 1/2 tại +2R (chỉ rule2)
            if rule == 2 and not half_done and hN[j] >= tp2:
                realized += 0.5 * (tp2 - entry) / entry
                rem = 0.5; half_done = True
                sl = max(sl, entry)  # phần còn lại về BE
            new_sl = hh - 3 * AN[j]
            if new_sl > sl:
                sl = new_sl
            if lN[j] <= sl:
                realized += rem * (sl - entry) / entry
                return realized - 2 * TAKER_FEE, j
            # time-stop
            if rule == 1 and j - e >= 10 and cN[j] <= entry:
                realized += rem * (cN[j] - entry) / entry
                return realized - 2 * TAKER_FEE, j
            if rule == 2 and (e50N[j] < e200N[j] or j - e >= 40):
                realized += rem * (cN[j] - entry) / entry
                return realized - 2 * TAKER_FEE, j
        last = min(e + 120, N - 1)
        realized += rem * (cN[last] - entry) / entry
        return realized - 2 * TAKER_FEE, last

    busy = -1
    for i in range(220, N - 1):
        if i <= busy or np.isnan(AN[i]) or np.isnan(e200N[i]) or np.isnan(donN[i]):
            continue
        uptrend = cN[i] > e200N[i] and e50N[i] > e200N[i]
        if not (uptrend and bull[i]):
            continue
        # RULE 1: Donchian-55 breakout (close mới = đỉnh 55 bar)
        if cN[i] >= donN[i] and cN[i] == max(cN[i - 54:i + 1]):
            entry = oN[i + 1] if i + 1 < N else cN[i]
            sl0 = entry - 3 * AN[i]
            if entry > sl0:
                pct, end = simulate(i + 1, entry, sl0, 1)
                trades.append({"sym": sym, "rule": 1, "t_in": times[i + 1], "t_out": times[end],
                               "pct": pct, "sl_dist": (entry - sl0) / entry})
                busy = end
                continue
        # RULE 2: pullback EMA20-50 rồi đóng lại trên EMA20, RSI giữ >40
        if lN[i] <= e20N[i] and cN[i] >= e50N[i] and cN[i] > oN[i] and cN[i] > e20N[i] \
                and not np.isnan(RN[i]) and RN[i] >= 40:
            entry = oN[i + 1] if i + 1 < N else cN[i]
            swing_low = lN[max(0, i - 5):i + 1].min()
            sl0 = max(swing_low, entry - 2 * AN[i])
            if entry > sl0:
                pct, end = simulate(i + 1, entry, sl0, 2)
                trades.append({"sym": sym, "rule": 2, "t_in": times[i + 1], "t_out": times[end],
                               "pct": pct, "sl_dist": (entry - sl0) / entry})
                busy = end
    return trades


def run_portfolio(cands, t0, t1):
    cands = sorted([c for c in cands if t0 <= c["t_in"] < t1], key=lambda c: c["t_in"])
    balance = INITIAL_BALANCE
    open_pos, trades = {}, []
    for c in cands:
        open_pos = {k: t for k, t in open_pos.items() if t > c["t_in"]}
        if c["sym"] in open_pos or len(open_pos) >= MAX_OPEN:
            continue
        notional = balance * RISK_PCT / c["sl_dist"]  # sizing theo rủi ro = SL distance
        balance += notional * c["pct"]
        trades.append({**c, "pnl": notional * c["pct"], "balance": balance})
        open_pos[c["sym"]] = c["t_out"]
    return pd.DataFrame(trades), balance


def report(name, tr, final, t0, t1):
    if tr.empty:
        print(f"  {name}: 0 lệnh")
        return
    days = (t1 - t0) / 86_400_000
    monthly = ((final / INITIAL_BALANCE) ** (30.44 / days) - 1) * 100 if final > 0 else float("nan")
    eq = tr["balance"]
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    pfd = abs(tr.loc[tr.pnl <= 0, "pnl"].sum())
    pf = tr.loc[tr.pnl > 0, "pnl"].sum() / pfd if pfd else float("inf")
    twin = tr.loc[tr.pnl > 0, "pnl"].mean()
    tloss = abs(tr.loc[tr.pnl <= 0, "pnl"].mean()) if (tr.pnl <= 0).any() else 0
    print(f"  {name}: vốn {final:>9,.0f} ({(final/INITIAL_BALANCE-1)*100:+6.1f}%) | LN/th {monthly:+.2f}% | "
          f"DD {dd:5.1f}% | WR {(tr.pnl>0).mean()*100:4.1f}% | PF {pf:.2f} | "
          f"win/loss {twin/tloss if tloss else 0:.2f} | {len(tr)} lệnh")


def main():
    print("V10 — LONG trend-following 4H (Donchian-55 + EMA-pullback), độc lập zone\n")
    allc = []
    for sym in COINS:
        t = signals(sym)
        allc += t
        r1 = sum(1 for x in t if x["rule"] == 1)
        print(f"  {sym:<10}: {r1:>3} breakout | {len(t)-r1:>3} pullback")
    for name, t0, t1 in [("IS 2020-2024", 0, CUT_OOS), ("OOS 2025-nay", CUT_OOS, 4_000_000_000_000)]:
        print(f"\n===== {name} =====")
        for label, pool in [("Rule1 breakout", [c for c in allc if c["rule"] == 1]),
                            ("Rule2 pullback", [c for c in allc if c["rule"] == 2]),
                            ("Cả hai (LONG trend)", allc)]:
            tr, final = run_portfolio(pool, t0, t1)
            ts = tr["t_in"] if not tr.empty else pd.Series([t0, t1])
            report(label, tr, final, ts.min(), ts.max())


if __name__ == "__main__":
    main()
