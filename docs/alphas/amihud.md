# amihud (G3) — diversifier thật

Alpha **uncorrelated thật duy nhất** trong bộ (|corr| ≤ 0.09 vs mọi thành viên trend). Thu **phần bù kém
thanh khoản**: trong top-180, long coin kém thanh khoản hơn, short coin thanh khoản nhất. Cơ chế không liên
quan tới trend giá, nên nó lãi khi trend đứng — và còn *mạnh hơn ngoài mẫu*.

| param | giá trị |
|---|---|
| khung | 4h · rebal 12 (2d) · vol-lookback 30 (5d) |
| signal | `cs_zscore(ts_mean(abs(returns_t180) / dollar_volume_t180, 180))` |
| định cỡ | trọng số độ lớn từng coin (winsor-cont, không rank) — xem [overview](overview.md) |
| universe | top-180 (`_t180`) · vol-target 10% · gross 1 trung hòa đô-la |
| hiệu năng | IS 1.86 · **OS 2.34 · WFE 1.26** · maxDD(OS) −12% · **182 lệnh/năm** (turnover rất thấp) |

## Tín hiệu & vào lệnh long/short

**Signal (điểm mỗi coin):** `ts_mean(abs(returns_t180)/dollar_volume_t180, 180)` = tỉ số kém thanh khoản Amihud (giá dịch chuyển bao nhiêu trên mỗi đô giao dịch), TB 30 ngày. Cao = kém thanh khoản.

**Vào lệnh (magnitude — bản đang giao):**
- `z = cs_zscore(signal)` — xếp độ kém thanh khoản trên TOÀN universe mỗi bar.
- **LONG** coin `z > 0` = **KÉM thanh khoản nhất** trong top-180 (thu phần bù thanh khoản) — long nặng nhất.
- **SHORT** coin `z < 0` = **thanh khoản nhất** (BTC/ETH...) — short nặng nhất.
- `weight = cs_scale(cs_winsorize(z, 3))` → |size| ∝ |z| (khoảng cách z-score so với TB rổ), kẹp ±3σ, gross 1, dollar-neutral.
- ⚠️ Hướng **phản trực giác**: long coin "khó mua" — xem capacity ở mục Caveat.


**Sizing ra đô-la:** `notional_i = weight_i × vốn × lev` (lev động bởi vol-target, Amihud ~0.49 trên $10k). Cách tính đầy đủ + ví dụ $10k + lọc min-order: **[sizing.md](sizing.md)**.

## Đọc công thức

1. `abs(returns) / dollar_volume` **theo coin, theo bar** = **tỉ số kém thanh khoản Amihud**: giá dịch chuyển
   bao nhiêu trên mỗi đô giao dịch. Cao = mỏng/kém thanh khoản (dòng tiền nhỏ đẩy giá); thấp = sâu/thanh khoản.
2. `ts_mean(..., 180)` = trung bình qua 180 bar (4h × 180 = **30 ngày**) → ước lượng kém thanh khoản ổn định,
   chậm cho mỗi coin.
3. `cs_zscore(...)` trên các coin → **long coin kém thanh khoản nhất (tỉ số cao), short coin thanh khoản nhất.**

`dollar_volume_t180` = quote-volume (turnover USDT) của top-180. `returns_t180` = lợi suất mỗi bar.

## Trực giác kinh tế

Amihud (2002): tài sản kém thanh khoản phải trả **phần bù lợi suất** để bù rủi ro price-impact cho người
nắm giữ. Ngay trong rổ thanh khoản vẫn có gradient thanh khoản, và các tên "đắt-để-bỏ-qua" trả thêm. Vì thứ
hạng kém thanh khoản đổi chậm, turnover cực nhỏ (182 lệnh/năm) → **gần như không hao phí** — sleeve rẻ nhất.

## Pseudocode cho dev

```python
D = 180                                                  # 30 ngày @ 4h
illiq = (returns.abs() / dollar_volume).rolling(D).mean()    # Amihud, theo coin
z = illiq.sub(illiq.mean(1),axis=0).div(illiq.std(1),axis=0) # cs_zscore (long illiq cao)
w = cs_scale(cs_winsorize(z, 3))                             # trọng số độ lớn
# rebalance 12, shift(1) exec-lag, vol-target 10%
```

## Hiệu năng & vai trò

**Viên ngọc để đa dạng hóa.** OS Sharpe 2.34 > IS 1.86 (WFE 1.26 — sống tốt hơn ngoài mẫu), **corr ~0.0–0.09
với toàn bộ cụm trend và −0.01 với G5**, và là sleeve duy nhất **dương qua chop 2023**. Vì thế nó là neo của
cả DECORRELATED-5 lẫn low-corr core.

## Caveat — đọc trước khi tăng size

- **Capacity là ràng buộc then chốt:** factor này *long chính các tên kém thanh khoản nhất* — đúng các tên
  hấp thụ size kém nhất. Nghiên cứu cho thấy phần bù kém thanh khoản tập trung ở micro-cap và **suy yếu trên
  rổ top-180 thanh khoản**, nên ĐỪNG scale sleeve này lớn; slippage sẽ ăn hết.
- Turnover thấp là điểm cộng (rẻ) nhưng nghĩa là tín hiệu chậm — không phản ứng kịp các cú đổi regime nhanh.
- Coi OS 2.34 là trần lạc quan (survivorship + selection); dù vậy, *sự decorrelation* mới là phần thưởng
  thật, không phải con số Sharpe nổi bật.

## Liên quan

- [overview](overview.md) · [trend-skew](trend-skew.md) (sleeve nửa-độc-lập còn lại)
- reference: [operators](../reference/operators.md) · [data](../reference/data.md) · [pipeline](../reference/pipeline.md)
