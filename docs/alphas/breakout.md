# breakout (S4 · S5)

Momentum theo vị trí range — long coin đang giao dịch gần **đỉnh range 30 ngày** (phá vỡ lên), short coin
gần đáy. Hai cách đo gần như giống hệt: S4 dùng range theo close, S5 dùng range high/low (Williams %R /
Stochastic).

| param | giá trị |
|---|---|
| định cỡ | trọng số độ lớn từng coin (winsor-cont, không rank) — xem [overview](overview.md) |
| universe | top-180 (`_t180`) · vol-target 10% · gross 1 trung hòa đô-la |
| hướng | **long gần đỉnh range (breakout), short gần đáy** (momentum) |

## Tín hiệu & vào lệnh long/short

**Signal (điểm mỗi coin):** `ts_scale(close_t180, 2880)` (S4) = vị trí close trong range 30 ngày, ∈ [0,1] (1 = đỉnh, 0 = đáy). S5 thay range close bằng range high/low (Williams %R).

**Vào lệnh (magnitude — bản đang giao):**
- `z = cs_zscore(signal)` — xếp vị-trí-range đó trên TOÀN universe mỗi bar.
- **LONG** coin `z > 0` (gần đỉnh range hơn trung bình rổ); coin **sát đỉnh range nhất** (breakout) = long **nặng nhất**.
- **SHORT** coin `z < 0` (gần đáy range); coin **sát đáy nhất** = short **nặng nhất**.
- `weight = cs_scale(cs_winsorize(z, 3))` → |size| ∝ |z| (khoảng cách z-score so với TB rổ), kẹp ±3σ, gross 1, dollar-neutral.

**Sizing ra đô-la:** `notional_i = weight_i × vốn × lev` (lev động bởi vol-target). Cách tính đầy đủ + ví dụ $10k + lọc min-order: **[sizing.md](sizing.md)**.


## Biến thể (cả hai 15m, rebal 192 / vol-lookback 480)

| key | signal | IS | OS | WFE | maxDD(OS) | lệnh/năm |
|---|---|---|---|---|---|---|
| S4 | `cs_zscore(ts_scale(close_t180, 2880))` | 2.12 | 1.63 | 0.77 | −15% | 2423 |
| S5 | `cs_zscore((close_t180 − ts_min(low_t180,2880)) / (ts_max(high_t180,2880) − ts_min(low_t180,2880) + 1e-9))` | 2.00 | 1.49 | 0.74 | −15% | 2421 |

## Đọc công thức

- **S4** `ts_scale(close, 2880)` = `(close − min_30d(close)) / (max_30d(close) − min_30d(close))` ∈ [0,1]
  → close đang ở đâu trong range-close 30 ngày của chính nó. **1 = chạm đỉnh 30 ngày, 0 = chạm đáy.**
- **S5** cùng ý nhưng kênh dựng từ **low** (sàn) và **high** (trần) → vị trí Williams %R / Stochastic kinh
  điển. Bắt range thật của nến, không chỉ close.
- `cs_zscore(...)` xếp hạng vị trí đó trên các coin → long coin gần đỉnh range nhất.

## Trực giác kinh tế

Breakout Donchian / Turtle: giá đẩy lên đỉnh range gần đây báo hiệu đổi regime và khởi đầu trend; momentum
có xu hướng đi theo. Dựa trên range (min/max) nên **ít nhạy với biến động** hơn trend-z (mean/std), do đó
hành xử hơi khác trong thị trường êm vs động.

## Pseudocode cho dev (S4)

```python
D = 2880                                              # 30 ngày @ 15m
lo, hi = close.rolling(D).min(), close.rolling(D).max()
pos = (close - lo) / (hi - lo + 1e-9)                 # ts_scale, theo coin, trong [0,1]
z   = pos.sub(pos.mean(1), axis=0).div(pos.std(1), axis=0)   # cs_zscore
w   = cs_scale(cs_winsorize(z, 3))                    # trọng số độ lớn
# S5: thay lo=low.rolling(D).min(), hi=high.rolling(D).max()
# sau đó rebalance-throttle 192, shift(1) exec-lag, vol-target 10%
```

## Hiệu năng & vai trò

WFE tốt nhất nhóm trend (0.77 / 0.74) — decay ngoài mẫu ít hơn trend-z thuần. **S4 và S5 corr 0.99** →
thực chất cùng một bet; chỉ ship **một** (S4 đơn giản hơn). Corr ~0.78–0.86 vs trend-z, nên breakout
**nửa dư thừa** với sleeve trend, không phải diversifier độc lập.

## Caveat

- S4 ≈ S5: đừng tính là hai sleeve.
- Tín hiệu range dễ phá-vỡ-giả trong chop (2023). Cụm stop-hunt/thanh lý dồn quanh các biên range hiển nhiên
  → thêm slippage khi size lớn — canh capacity ở các coin `_t180` kém thanh khoản.

## Liên quan

- [overview](overview.md) · [trend-z](trend-z.md) · [trend-breakout](trend-breakout.md) (S3 = trend-z + cái này)
- reference: [operators](../reference/operators.md) · [pipeline](../reference/pipeline.md)
