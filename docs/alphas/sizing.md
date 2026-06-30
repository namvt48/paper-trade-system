# Sizing (định cỡ vị thế)

Cách biến `signal` của một alpha thành **số đô-la phải hold mỗi coin**. Dùng chung cho **cả 5 alpha new** —
chỉ `signal` khác nhau, các bước sizing giống hệt. (Khác alpha live decile: ở đây mọi coin có size theo độ mạnh.)

## 5 bước tính

```
Bước 1: signal_i   = điểm thô coin i (trend z-score / Amihud / breakout / ...)
Bước 2: z_i        = cs_zscore(signal) = (signal_i − TB_rổ) / std_rổ      # mỗi bar, ngang các coin
Bước 3: z_i        = clip(z_i, −3, +3)            # cs_winsorize: chặn coin outlier
Bước 4: w_i        = z_i / Σ_j|z_j|               # cs_scale: Σ|w|=1 (gross 1), Σw≈0 (dollar-neutral)
Bước 5: notional_i = w_i × VỐN × lev              # lev = đòn bẩy vol-target
```

- `z_i` = coin lệch **bao nhiêu σ so với TRUNG BÌNH rổ** → lệch xa = size nặng. (Đây là "độ lệch z-score", **không phải skew**.)
- `Σw ≈ 0` → long ≈ short tự động → **dollar-neutral**.

## Ví dụ toy (5 coin)

signal thô `[3.0, 1.0, 0.0, −1.0, −3.0]` → TB=0, std=2.0 → z `[1.5, 0.5, 0, −0.5, −1.5]`, Σ|z|=4.0:

| coin | z | w = z/Σ\|z\| | với $10k, lev 1.5× |
|---|---|---|---|
| A | +1.5 | **+0.375** | LONG $5.625 |
| B | +0.5 | +0.125 | LONG $1.875 |
| C | 0.0 | 0.00 | ~0 |
| D | −0.5 | −0.125 | SHORT $1.875 |
| E | −1.5 | **−0.375** | SHORT $5.625 |

→ lệch xa nhất trên TB = long nặng nhất; xa nhất dưới TB = short nặng nhất; gần TB ≈ 0. Long $7.5k = short $7.5k, gross $15k, net 0.

## Đòn bẩy vol-target (lev) — ĐỘNG

```
realized_vol = std(pnl_gross-1, vol-lookback) × √(bars/năm)     # vd 5 ngày
lev          = clip(0.10 / realized_vol, ≤ 3.0)                 # ghì vol thực về ~10%/năm
```

- lev đổi **mỗi bar**: vol thấp → lev cao; vol cao → lev thấp. **Re-lever chỉ tại nhịp rebalance** (không mỗi bar — xem mục thực thi).
- Thực tế (bar gần nhất, $10k): **trend lev ~0.18**, **Amihud lev ~0.49** (Amihud vol thấp hơn → lev cao hơn).
- → gross thực chỉ **18–49% vốn**, KHÔNG dùng hết — còn nhiều room nâng target_vol. Tăng target_vol = nhân tuyến tính lãi+lỗ, Sharpe không đổi:

| target_vol | real_vol | CAGR | maxDD | (max_lev=3) |
|---|---|---|---|---|
| 0.10 (hiện tại) | 11% | 26% | −10% | lev TB 0.38 |
| 0.20 | 22% | 57% | −19% | chưa chạm trần |
| 0.30 | 33% | 94% | −28% | chưa chạm trần |

## Ví dụ THẬT $10k (trọng số bar gần nhất)

**Trend (4h):** lev 0.18 → gross $1.774 | 67L/132S | top long UAI/BEAT/STG ~$45 | top short COW −$32. Lớn nhất ~$45.

**Amihud (4h):** lev 0.49 → gross $4.881 | 95L/104S | top long SAFE $108/BRETT $98/CVX $95 (alt kém thanh khoản) | top short **BTC/ETH/SOL/XRP/BNB mỗi cái −$37** (major thanh khoản).

Tính 1 vị thế: `notional_BTC = w_BTC × $10.000 × lev = (−0.0076) × 10000 × 0.49 = −$37`.

## Từ signal → LỆNH thực (quan trọng)

- **Lần đầu (từ tiền mặt):** ~200 lệnh build cả sổ (magnitude phủ ~cả pool).
- **Mỗi rebalance sau:** chỉ trade phần CHÊNH `lệnh_i = target_i − current_i` (diff-to-target). Signal chậm → **~33 lệnh/rebalance** với band, KHÔNG phải 200.
- **No-trade band:** bỏ qua `|Δnotional| < 0.4% vốn` (≈ $40 trên $10k).
- **Min-order floor:** coin có `|notional| < min-order sàn` → không mở. Trên $10k lọc còn **~30–54 vị thế** đáng giao dịch.
- **Nhịp rebalance:** 15m/1h/4h = mỗi 2 ngày, 1d = mỗi ngày. **KHÔNG re-lever mỗi bar** (đó là bug over-trade).

## Liên quan

- alpha: [trend-z](trend-z.md) · [breakout](breakout.md) · [trend-breakout](trend-breakout.md) · [amihud](amihud.md) · [trend-skew](trend-skew.md)
- [overview](overview.md) · reference: [pipeline](../reference/pipeline.md) · [operators](../reference/operators.md)
