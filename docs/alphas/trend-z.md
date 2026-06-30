# trend-z (S1 · S2 · G1 · G2)

Factor trend lõi — **một tín hiệu chạy ở bốn khung**. Long coin có uptrend mạnh nhất so với chính lịch sử
30 ngày của nó (và so với rổ coin), short coin yếu nhất.

| param | giá trị |
|---|---|
| signal | `cs_zscore(ts_zscore(close_t180, D))`, D = 30 ngày tính theo bar |
| định cỡ | trọng số độ lớn từng coin (winsor-cont, không rank) — xem [overview](overview.md) |
| universe | top-180 theo thanh khoản trượt (`_t180`) |
| exec_lag | 1 bar · vol-target 10%/năm · max_lev 3 · trung hòa đô-la gross 1 |
| hướng | **long z cao (uptrend), short z thấp** (momentum) — KHÔNG phải hướng contrarian trong doc cũ |

## Tín hiệu & vào lệnh long/short

**Signal (điểm mỗi coin):** `ts_zscore(close_t180, D)` = độ mạnh uptrend của coin (giá so với TB 30 ngày của chính nó, chuẩn hóa theo vol riêng). D = 30 ngày theo bar.

**Vào lệnh (magnitude — bản đang giao):**
- `z = cs_zscore(signal)` — xếp độ mạnh trend đó trên TOÀN universe mỗi bar.
- **LONG** coin `z > 0` (trend mạnh hơn trung bình rổ); coin uptrend **mạnh nhất** = long **nặng nhất**.
- **SHORT** coin `z < 0` (yếu hơn rổ); coin **yếu nhất** = short **nặng nhất**.
- `weight = cs_scale(cs_winsorize(z, 3))` → |size| ∝ |z| (khoảng cách z-score so với TB rổ — KHÔNG phải skew), kẹp ±3σ, gross 1, dollar-neutral (long = short).

**Sizing ra đô-la:** `notional_i = weight_i × vốn × lev` (lev động bởi vol-target, trend ~0.18 trên $10k). Cách tính đầy đủ + ví dụ $10k + lọc min-order: **[sizing.md](sizing.md)**.


## Biến thể theo khung

| key | khung | D (bar) | = ngày | rebal | vol-lookback | IS | OS | WFE | maxDD(OS) |
|---|---|---|---|---|---|---|---|---|---|
| S1 | 15m | 2880 | 30 | 192 (2d) | 480 (5d) | 2.68 | 1.37 | 0.51 | −13% |
| S2 | 1h | 720 | 30 | 48 (2d) | 120 (5d) | 2.67 | 1.28 | 0.48 | −13% |
| G1 | 4h | 180 | 30 | 12 (2d) | 30 (5d) | 2.11 | 1.44 | 0.68 | −18% |
| G2 | 1d | 30 | 30 | 1 | 30 | 2.07 | 1.28 | 0.62 | −12% |

## Đọc công thức

1. `ts_zscore(close, D)` = `(close − mean_D(close)) / std_D(close)` **theo từng coin** → giá hiện tại cao/thấp
   hơn mức trung bình 30 ngày của chính nó bao nhiêu σ = độ mạnh trend, đã chuẩn hóa theo vol riêng của coin
   (để coin êm và coin biến động mạnh so sánh được với nhau).
2. `cs_zscore(...)` = z-score giá trị trend đó **trên tất cả các coin tại mỗi bar** → coin nào đang trend
   mạnh nhất so với rổ ngay lúc này.
3. Định cỡ độ lớn (overview) biến z cross-sectional thành trọng số trung hòa đô-la, gross-1 cho mỗi coin.

## Trực giác kinh tế

Trend crypto dai dẳng (phản ứng chậm với tin tức + dòng vốn xoay vòng chậm). Chuẩn hóa hai lần (theo vol
riêng rồi cross-sectional) giữ tín hiệu sạch: coin vol cao không lấn át, và bet thuần túy là độ mạnh trend
*tương đối*, không phải mức giá tuyệt đối.

## Pseudocode cho dev

```python
D   = {"15m":2880, "1h":720, "4h":180, "1d":30}[tf]          # 30 ngày
trend = (close - close.rolling(D).mean()) / close.rolling(D).std()   # ts_zscore, theo coin
z     = trend.sub(trend.mean(1), axis=0).div(trend.std(1), axis=0)   # cs_zscore, trên các coin
zw    = z.clip(z.mean(1)-3*z.std(1), z.mean(1)+3*z.std(1), axis=0)   # cs_winsorize ±3σ
w     = zw.div(zw.abs().sum(1), axis=0)                              # cs_scale → gross 1, trung hòa
# sau đó: rebalance-throttle mỗi `rebal` bar (ffill), shift(1) exec-lag, vol-target 10%
```

## Hiệu năng & vai trò

Sharpe IS cao nhất bộ (S1 2.68) nhưng **WFE 0.51 / 0.48 = borderline** — con số IS bị thổi phồng do
selection; OS 1.3–1.4 mới là con số trung thực, vẫn giao dịch được. **S1≈S2 (corr 0.99), cả bốn tương quan
0.86–0.99** → đây là MỘT bet ở bốn khung, không phải bốn diversifier. Chạy **một** khung (15m cho năng động,
1d cho turnover thấp) trừ khi bạn cố ý muốn trung bình hóa nhiễu đa khung.

## Caveat

- Yếu khi chop: 2023 ≈ +0.2. Edge thuần trend-regime.
- Biến thể 4h (G1) có drawdown OS sâu nhất (−18%) — khung chậm, breadth mỏng hơn.
- Chỉ dùng MỘT cái trong số này làm "sleeve trend"; xếp cả bốn không hề đa dạng hóa.

## Liên quan

- [overview](overview.md) · [trend-breakout](trend-breakout.md) (thêm xác nhận thứ 2) · [breakout](breakout.md)
- reference: [operators](../reference/operators.md) · [pipeline](../reference/pipeline.md) · [entry-exit](../reference/entry-exit.md)
