# funding-onchain (N1)

Bet **MỚI hoàn toàn** — KHÔNG dùng giá/khối lượng. So **tâm lý đám đông phái sinh** (funding rate) với
**sức khỏe mạng thật** (số ví hoạt động). Trực giao 100% với mọi alpha giá (corr ≤ 0.07 với trend/breakout/amihud).

| param | giá trị |
|---|---|
| signal | `cs_zscore(ts_ema(funding_zscore21, 60)) − cs_zscore(active_users)` |
| khung | 1d |
| định cỡ | magnitude (cs_scale(cs_winsorize(z,3))) — xem [sizing](sizing.md) |
| universe | giao của coin có funding (115) ∩ có DAU on-chain (53) ≈ **30 coin** |
| data | `funding_zscore21` (lệch chuẩn phí funding) + `active_users` (ví hoạt động/ngày, Artemis) |
| lịch sử | từ 2023-07 (funding) → dùng walk-forward, không đủ IS 3 năm |

## Tín hiệu & vào lệnh long/short

**Signal (điểm mỗi coin):** so sánh hai z-score:
- `funding_zscore21` = phí funding của coin đang lệch chuẩn bao nhiêu (cao = đám đông phái sinh đang hưng phấn).
- `active_users` = số ví hoạt động thật mỗi ngày (cao = mạng khỏe).

`z = cs_zscore(ts_ema(funding_zscore21,60)) − cs_zscore(active_users)` rồi xếp trên toàn universe mỗi bar.

- **LONG** coin **người dùng thật ĐÔNG** nhưng **funding CHƯA hưng phấn** (giá trị thật chưa bị thổi) — long nặng nhất.
- **SHORT** coin **funding cao** (bị thổi) mà **ít người dùng thật** (rỗng) — short nặng nhất.
- `weight = cs_scale(cs_winsorize(z, 3))` → gross 1, dollar-neutral.

## Trực giác kinh tế
Công ty làm ăn thật tốt (nhiều người dùng) mà cổ phiếu chưa bị đầu cơ thổi giá = món hời. Đây là bet
"giá trị nền tảng vs đầu cơ" — chỉ thấy được khi ghép data on-chain (người dùng) với data phái sinh (funding),
hoàn toàn nằm ngoài giá.

## Hiệu năng & vai trò
- Net Sharpe (sau phí 15bps) ≈ **1.01** · OS ≈ 1.03 · WFE ≈ **1.01** (không decay).
- Turnover ~14%/ngày · giữ ~30 coin · ~20 coin giao dịch/ngày.
- breakeven ~68bps · cluster-neutral vẫn dương (+0.11).
- **Vai trò: diversifier ĐỘC LẬP NHẤT cả bộ** (corr ≤ 0.26 với mọi alpha khác). Đây là viên ngọc đa dạng hóa.

## Caveat
- Lịch sử ngắn (funding 2023+) → OS ngắn, dùng walk-forward.
- Coverage MỎNG (~30 coin) → breadth thấp, biến động ước lượng cao hơn các alpha giá (200 coin).
- OS đã bị nhòm khi mining → cần forward paper-trade trước khi tin.

## Liên quan
- [amihud](amihud.md) · [overview](overview.md) · [TONG_HOP_DE_HIEU](../TONG_HOP_DE_HIEU.md)
- data: `derived/funding_termstructure/`, `derived/onchain_broad/active_users` · [GIAI_THICH_DATA](../../../datacryp/GIAI_THICH_DATA.md)
