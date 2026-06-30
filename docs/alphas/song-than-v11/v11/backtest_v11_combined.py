"""
Song Than V11 — DUAL-SIDE gộp (SHORT V7 15m + LONG trend V10 4h) + lớp quản trị rủi ro.

Mục tiêu: GIỮ rủi ro thấp (MaxDD mục tiêu <=20%) chứ không tối đa return.

Tín hiệu nền:
  - SHORT: V7 breakdown-continuation (15m), SL coin 1.75/2.5%, TP x1.43, hold 64
  - LONG : V10 trend-following (4h Donchian-55 + EMA pullback), trailing chandelier

Mỗi tín hiệu được gắn REGIME của coin tại thời điểm vào (bull/bear/sideways).

Các lớp overlay (bật/tắt để ablation):
  R1 regime-size : bull→long x1.0/short x0.5 | bear→short x1.0/long x0 | sideways→cả hai x0.5
  R2 vol-target  : đã ngầm có (size = risk%/SL_dist) — chuẩn hoá rủi ro mỗi lệnh
  R3 global cap  : tối đa MAX_OPEN vị thế mở
  R4 dd-derisk   : nếu equity < 0.85*peak → size x0.5 đến khi hồi

Chạy:  python3 backtest_v11_combined.py
"""

import numpy as np
import pandas as pd

import backtest_v9_regime as v9
import backtest_v10_trend as v10
from backtest_v9_regime import compute_swing_points, compute_trailing, load as load15

INIT = 10_000.0
BASE_RISK = 0.004      # rủi ro nền 0.4% equity/lệnh
MAX_OPEN = 5
CUT_OOS = 1735689600000
COINS = list(v9.COINS.items())


def regime_series(sym):
    """Regime daily (ffill về timestamp ms-day) cho 1 coin."""
    df = load15(sym)
    d = df.copy()
    d["day"] = d["open_time"] // 86_400_000
    daily = d.groupby("day")["close"].last()
    e200 = daily.ewm(span=200, adjust=False).mean()
    e50 = daily.ewm(span=50, adjust=False).mean()
    ret60 = daily / daily.shift(60) - 1
    slope = e200 / e200.shift(20) - 1
    reg = pd.Series("SIDEWAYS", index=daily.index)
    reg[(daily < e200) & (e50 < e200) & (ret60 < 0)] = "BEAR"
    reg[(daily > e200) & (e50 > e200) & (ret60 > 0) & (slope > 0)] = "BULL"
    return reg  # index = day-number


def regime_at(reg, t_ms):
    return reg.get(t_ms // 86_400_000, "SIDEWAYS")


def build_signals():
    shorts, longs = [], []
    regs = {}
    for sym, sl in COINS:
        reg = regime_series(sym)
        regs[sym] = reg
        for s in v9.gen_signals_v7(sym, sl):
            s.update(sym=sym, kind="SHORT", risk_unit=sl, regime=regime_at(reg, int(s["t_in"])))
            shorts.append(s)
        for c in v10.signals(sym):
            c.update(kind="LONG", risk_unit=c["sl_dist"], regime=regime_at(reg, int(c["t_in"])))
            longs.append(c)
    return shorts, longs


def size_mult(kind, regime, overlays):
    m = 1.0
    if "R1" in overlays:
        if regime == "BULL":
            m = 1.0 if kind == "LONG" else 0.5
        elif regime == "BEAR":
            m = 0.0 if kind == "LONG" else 1.0
        else:  # SIDEWAYS / chop
            m = 0.5
    return m


def run(cands, overlays, t0, t1):
    cands = sorted([c for c in cands if t0 <= c["t_in"] < t1], key=lambda c: c["t_in"])
    bal = INIT
    peak = INIT
    op = {}
    rows = []
    for c in cands:
        op = {k: t for k, t in op.items() if t > c["t_in"]}
        if c["sym"] in op or len(op) >= MAX_OPEN:
            continue
        m = size_mult(c["kind"], c["regime"], overlays)
        if m <= 0:
            continue
        if "R4" in overlays and bal < 0.85 * peak:
            m *= 0.5
        notional = bal * BASE_RISK * m / c["risk_unit"]
        pnl = notional * c["pct"]
        bal += pnl
        peak = max(peak, bal)
        rows.append({**c, "pnl": pnl, "bal": bal})
        op[c["sym"]] = c["t_out"]
    return pd.DataFrame(rows)


def metrics(tr):
    if tr.empty:
        return None
    eq = tr["bal"]
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    pfd = abs(tr.loc[tr.pnl <= 0, "pnl"].sum())
    pf = tr.loc[tr.pnl > 0, "pnl"].sum() / pfd if pfd else float("inf")
    ret = (eq.iloc[-1] / INIT - 1) * 100
    # Calmar thô = return / |maxDD|
    calmar = ret / abs(dd) if dd else float("inf")
    return dict(ret=ret, dd=dd, pf=pf, n=len(tr), wr=(tr.pnl > 0).mean() * 100, calmar=calmar)


def main():
    print("V11 — DUAL-SIDE gộp + quản trị rủi ro (mục tiêu MaxDD thấp)\n")
    shorts, longs = build_signals()
    allc = shorts + longs
    print(f"Tín hiệu: {len(shorts)} SHORT + {len(longs)} LONG = {len(allc)}\n")

    configs = {
        "Baseline (không overlay)":      set(),
        "+R1 regime-size":               {"R1"},
        "+R1+R4 dd-derisk":              {"R1", "R4"},
        "Short-only (tham chiếu)":       "SHORT_ONLY",
    }
    for full, t0, t1 in [("TOÀN CHU KỲ", 0, 4e12), ("IS 2020-24", 0, CUT_OOS), ("OOS 2025+", CUT_OOS, 4e12)]:
        print(f"===== {full} =====")
        print(f"  {'cấu hình':<26} {'return':>8} {'MaxDD':>7} {'PF':>5} {'WR':>6} {'Calmar':>7} {'lệnh':>6}")
        for name, ov in configs.items():
            pool = shorts if ov == "SHORT_ONLY" else allc
            ovset = set() if ov == "SHORT_ONLY" else ov
            m = metrics(run(pool, ovset, t0, t1))
            if m:
                print(f"  {name:<26} {m['ret']:>+7.0f}% {m['dd']:>6.1f}% {m['pf']:>5.2f} {m['wr']:>5.1f}% {m['calmar']:>7.2f} {m['n']:>6}")
        print()


if __name__ == "__main__":
    main()
