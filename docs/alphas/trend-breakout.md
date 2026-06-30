# trend-breakout (S3)

Một blend: **trend dài hạn được xác nhận bởi breakout range.** Cộng hai z-score độc lập — một coin phải VỪA
trong uptrend 120 ngày VỪA gần đỉnh range 30 ngày mới được long nặng. Xác nhận kép này cho S3 hồ sơ decay
tốt nhất nhóm trend (WFE 0.71).

| param | giá trị |
|---|---|
| khung | 1h · rebal 48 (2d) · vol-lookback 120 (5d) |
| signal | `cs_zscore(ts_zscore(close_t180, 2880)) + cs_zscore(ts_scale(close_t180, 720))` |
| định cỡ | trọng số độ lớn từng coin (winsor-cont, không rank) — xem [overview](overview.md) |
| universe | top-180 (`_t180`) · exec_lag 1 · vol-target 10% · gross 1 trung hòa đô-la |
| hiệu năng | IS 2.38 · OS 1.70 · WFE 0.71 · maxDD(OS) −10% · 1903 lệnh/năm |

## Tín hiệu & vào lệnh long/short

**Signal (điểm mỗi coin):** `cs_zscore(ts_zscore(close,2880)) + cs_zscore(ts_scale(close,720))` = (trend 120d) + (vị trí range 30d), cộng hai z-score.

**Vào lệnh (magnitude — bản đang giao):**
- `z = cs_zscore(signal_tổng)` — xếp điểm tổng trên TOÀN universe mỗi bar.
- **LONG** coin `z > 0`; coin **vừa uptrend dài hạn mạnh vừa gần đỉnh range** = long **nặng nhất**.
- **SHORT** coin `z < 0`; coin **vừa downtrend vừa gần đáy range** = short **nặng nhất**.
- `weight = cs_scale(cs_winsorize(z, 3))` → |size| ∝ |z| (khoảng cách z-score so với TB rổ), kẹp ±3σ, gross 1, dollar-neutral.

**Sizing ra đô-la:** `notional_i = weight_i × vốn × lev` (lev động bởi vol-target). Cách tính đầy đủ + ví dụ $10k + lọc min-order: **[sizing.md](sizing.md)**.


## Đọc công thức

Hai vế, mỗi vế biến thành z-score cross-sectional, rồi **cộng lại** (chia đều):

1. **Vế A — trend (120d):** `cs_zscore(ts_zscore(close_t180, 2880))`. Ở 1h, 2880 bar = **120 ngày**
   (lưu ý: cùng `2880` nhưng = 30d ở 15m và 120d ở 1h). Độ mạnh trend dài hạn, chuẩn hóa theo vol riêng.
2. **Vế B — breakout (30d):** `cs_zscore(ts_scale(close_t180, 720))`. 720 bar @ 1h = 30 ngày. Vị trí của
   close trong range 30 ngày, z cross-sectional.
3. **Cộng** A + B → một coin điểm cao chỉ khi nó *vừa* là trend dài hạn mạnh *vừa* đang breakout ngắn hạn.
   Sau đó định cỡ độ lớn → trọng số trung hòa đô-la gross-1.

## Trực giác kinh tế

Kết hợp trend chậm với breakout nhanh lọc bỏ (a) trend dài đã chững (A cao, B thấp) và (b) cú pop ngắn không
có trend phía sau (A thấp, B cao). Yêu cầu cả hai = ít tín hiệu giả → **ít overfit, decay ngoài mẫu dịu hơn**
so với từng vế riêng (S2 trend WFE 0.48 vs S3 0.71).

## Pseudocode cho dev

```python
trend = (close - close.rolling(2880).mean()) / close.rolling(2880).std()   # 120d @ 1h
A = trend.sub(trend.mean(1),axis=0).div(trend.std(1),axis=0)               # cs_zscore
lo, hi = close.rolling(720).min(), close.rolling(720).max()                # 30d @ 1h
pos = (close - lo) / (hi - lo + 1e-9)
B = pos.sub(pos.mean(1),axis=0).div(pos.std(1),axis=0)                     # cs_zscore
sig = A + B                                                                # blend chia đều
w   = cs_scale(cs_winsorize(sig, 3))                                       # trọng số độ lớn
# rebalance 48, shift(1) exec-lag, vol-target 10%
```

## Hiệu năng & vai trò

**Thành viên đơn lẻ robust nhất của cụm trend** (WFE 0.71, OS 1.70, drawdown nhóm super nông nhất −10%).
Vẫn tương quan với trend thuần (0.76–0.82) và với breakout (0.81), nên nó là **đại diện tốt nhất của bet
trend**, không phải diversifier thêm. Trong ensemble low-corr core (S3 + G3 + G5) nó là vế trend → bộ ba đó
đạt OS Sharpe 2.88.

## Caveat

- Vẫn là bet trend-regime → mềm trong chop 2023.
- Hai vế dùng chung chuỗi giá; "độc lập" của A và B chỉ một phần, không trực giao.

## Liên quan

- [overview](overview.md) · [trend-z](trend-z.md) (vế A) · [breakout](breakout.md) (vế B) · [trend-skew](trend-skew.md)
- reference: [operators](../reference/operators.md) · [pipeline](../reference/pipeline.md)
