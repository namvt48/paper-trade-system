# amihud-lowbeta (N3)

Bet **MỚI** — nâng cấp Amihud (thanh khoản) bằng chiều **phòng thủ** (downside-beta thấp). Long coin vừa
KÉM thanh khoản vừa ÍT rớt khi thị trường rớt. Trực giao trend/breakout (corr 0.02/0.17); bổ sung amihud thuần.

| param | giá trị |
|---|---|
| signal | `cs_zscore(amihud) − cs_zscore(ts_ema(downside_beta, 90))` |
| khung | 1d |
| định cỡ | magnitude (cs_scale(cs_winsorize(z,3))) — xem [sizing](sizing.md) |
| universe | top-200 theo thanh khoản ≈ **84 coin** giữ vị thế |
| data | `amihud` (kém thanh khoản) + `downside_beta` (rớt mạnh cỡ nào khi thị trường rớt) |

## Tín hiệu & vào lệnh long/short

**Hai thành phần:**
- `amihud` = `mean(|return| / $vol)` → cao = **kém thanh khoản** (giá nhúc nhích mạnh trên mỗi đô giao dịch).
- `downside_beta` = coin rớt mạnh cỡ nào khi cả thị trường rớt. Thấp = **phòng thủ** (ít rớt theo).

`z = cs_zscore(amihud) − cs_zscore(ts_ema(downside_beta,90))` xếp trên toàn universe mỗi bar.

- **LONG** coin **kém thanh khoản + downside-beta THẤP** (thu phần bù thanh khoản, nhưng phòng thủ) — long nặng nhất.
- **SHORT** coin **thanh khoản nhất + rớt mạnh theo thị trường** (BTC/ETH beta cao) — short nặng nhất.
- `weight = cs_scale(cs_winsorize(z, 3))` → gross 1, dollar-neutral.

## Trực giác kinh tế
Amihud thuần (long hàng khó mua) ăn phần bù thanh khoản nhưng có thể rớt sâu lúc thị trường hoảng. Trừ thêm
`downside_beta` → ưu tiên những coin kém thanh khoản NHƯNG phòng thủ → giữ phần bù thanh khoản mà bớt rủi ro đuôi.

## Hiệu năng & vai trò
- Net Sharpe (sau phí 15bps) ≈ **1.59** (bản cluster-neutral) · OS ≈ 1.71 · WFE ≈ 1.1.
- Turnover ~4%/ngày · giữ ~84 coin · ~9 coin giao dịch/ngày · breakeven ~259bps (cost-robust mạnh).
- **cluster-neutral còn LÀM TỐT HƠN** (1.0 → 1.6) — dấu hiệu robust thật.
- **Vai trò: chiều illiquidity-phòng-thủ**, bổ sung amihud thuần (corr với amihud thuần ~0.1 sau khi thêm beta).

## Caveat
- corr 0.41 với họ 52w-high → nếu đã có `52w-high.md`, đây vẫn vừa đủ độc lập (0.38 với `uid_vs_disthigh`).
- `downside_beta` cuộn 90 ngày → tín hiệu chậm; phù hợp turnover thấp.

## Liên quan
- [amihud](amihud.md) (bản thuần, không có chiều beta) · [overview](overview.md) · [TONG_HOP_DE_HIEU](../TONG_HOP_DE_HIEU.md)
