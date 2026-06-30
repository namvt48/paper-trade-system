# 52w-high (N2)

Bet **MỚI** theo chiều "vị trí so với đỉnh lịch sử" (52-week-high) — chiều mà alpha_test cũ bỏ sót. Kết hợp
khoảng cách tới đỉnh với dòng thông tin nội ngày. Trực giao với trend/breakout/amihud (corr ≤ 0.22).

| param | giá trị |
|---|---|
| signal | `cs_zscore(ts_ema(uid, 10)) − cs_zscore(dist_high)` |
| khung | 1d |
| định cỡ | magnitude (cs_scale(cs_winsorize(z,3))) — xem [sizing](sizing.md) |
| universe | top-200 có data 1m (cho `uid`) ≈ **43 coin** |
| data | `dist_high` (giá cách đỉnh cuộn bao xa) + `uid` (entropy phân bố thông tin nội ngày, từ data 1m) |

## Tín hiệu & vào lệnh long/short

**Hai thành phần:**
- `dist_high` = `close / max(close, W) − 1` → 0 = đang ở đỉnh, càng âm = càng xa đỉnh. **Gần đỉnh = đang khỏe**
  (hiệu ứng 52-week-high: coin gần đỉnh lịch sử thường chạy tiếp).
- `uid` = thông tin trong ngày **rải đều** (cao) hay **dồn cục** vào vài phút (thấp). Rải đều = giao dịch lành mạnh.

`z = cs_zscore(ts_ema(uid,10)) − cs_zscore(dist_high)` xếp trên toàn universe mỗi bar.

- **LONG** coin **gần đỉnh + thông tin rải đều lành mạnh** — long nặng nhất.
- **SHORT** coin **xa đỉnh + thông tin dồn cục (pump giật)** — short nặng nhất.
- `weight = cs_scale(cs_winsorize(z, 3))` → gross 1, dollar-neutral.

## Trực giác kinh tế
Coin sát đỉnh lịch sử thường có momentum bền (ít người kẹt hàng phía trên). Lọc thêm bằng `uid` để tránh
những cú gần-đỉnh nhờ pump giật một vài phút (thông tin dồn cục) — chỉ giữ những coin gần đỉnh "thật".

## Hiệu năng & vai trò
- Net Sharpe (sau phí 15bps) ≈ **1.15** · OS ≈ 2.34 · WFE ≈ 3.1 (OS > IS — kiểm regime, nhưng cluster-robust).
- Turnover ~14%/ngày · giữ ~43 coin · ~28 coin giao dịch/ngày (active nhất bộ).
- breakeven ~59bps · cluster-neutral vẫn dương (+0.92).
- **Vai trò: chiều price-structure độc lập** với trend (corr 0.01) và breakout (0.22).

## Caveat
- `uid` cần data 1m → chỉ ~43 coin có.
- WFE > 1 (OS mạnh hơn IS) → một phần có thể là regime; cluster-neutral dương là điểm trấn an.
- Turnover cao hơn các factor chậm (amihud) → nhạy phí hơn (vẫn an toàn vì breakeven 59bps).

## Liên quan
- biến thể cùng họ (corr 0.37-0.51): `iskew_vs_disthigh`, `disthigh` solo — giữ MỘT đại diện (đây).
- [breakout](breakout.md) (cũng "gần đỉnh" nhưng khác cách đo) · [overview](overview.md) · [TONG_HOP_DE_HIEU](../TONG_HOP_DE_HIEU.md)
