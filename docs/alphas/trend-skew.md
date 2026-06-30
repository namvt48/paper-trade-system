# trend-skew (G5) — trend tránh coin lottery

Một blend thêm **bộ lọc độ lệch (skew)** vào trend dài hạn: long coin trong uptrend mà **không** phải tên
lottery lệch phải, short coin lệch phải mà không có uptrend. Nửa độc lập với cụm trend thuần (|corr|
0.49–0.73) và uncorrelated với Amihud (−0.01). WFE ngoài mẫu tốt nhất cả bộ (1.43) và drawdown nông nhất (−6%).

| param | giá trị |
|---|---|
| khung | 1h · rebal 48 (2d) · vol-lookback 120 (5d) |
| signal | `cs_zscore(ts_zscore(close_t180, 2880)) + cs_zscore(-ts_skew(returns_t180, 1440))` |
| định cỡ | trọng số độ lớn từng coin (winsor-cont, không rank) — xem [overview](overview.md) |
| universe | top-180 (`_t180`) · exec_lag 1 · vol-target 10% · gross 1 trung hòa đô-la |
| hiệu năng | IS 1.66 · **OS 2.37 · WFE 1.43** · maxDD(OS) **−6%** · 1283 lệnh/năm |

## Tín hiệu & vào lệnh long/short

**Signal (điểm mỗi coin):** `cs_zscore(ts_zscore(close,2880)) + cs_zscore(-ts_skew(returns,1440))` = (trend 120d) + (skew âm 60d), cộng hai z-score.

**Vào lệnh (magnitude — bản đang giao):**
- `z = cs_zscore(signal_tổng)` — xếp điểm tổng trên TOÀN universe mỗi bar.
- **LONG** coin `z > 0` = **uptrend + skew âm** (đều đặn, không lottery) — long nặng nhất.
- **SHORT** coin `z < 0` = **downtrend + skew dương** (pump giật, lottery) — short nặng nhất.
- `weight = cs_scale(cs_winsorize(z, 3))` → |size| ∝ |z| (khoảng cách z-score so với TB rổ), kẹp ±3σ, gross 1, dollar-neutral.

**Sizing ra đô-la:** `notional_i = weight_i × vốn × lev` (lev động bởi vol-target). Cách tính đầy đủ + ví dụ $10k + lọc min-order: **[sizing.md](sizing.md)**.


## Đọc công thức

Hai vế cộng lại (chia đều):

1. **Vế A — trend (120d):** `cs_zscore(ts_zscore(close_t180, 2880))`. 2880 bar @ 1h = 120 ngày. Độ mạnh
   trend dài hạn, chuẩn hóa theo vol riêng, z cross-sectional.
2. **Vế B — skew âm (60d):** `cs_zscore(-ts_skew(returns_t180, 1440))`. `ts_skew(returns, 1440)` = độ lệch
   của lợi suất qua 1440 bar (60 ngày). **Dấu trừ đảo nó lại**: long coin **skew âm** (đuôi trái, đều đặn),
   short coin **skew dương** (lottery, pump giật). Rồi z cross-sectional.
3. **Cộng** A + B → long một uptrend đều đặn, phạt một coin dễ-pump dù nó đang trend.

## Trực giác kinh tế

Skewness preference (Bali/Kumar): nhà đầu tư **trả giá cao cho payoff lệch phải kiểu lottery** → các coin đó
sau đó **underperform**. Short skew dương và nghiêng về skew âm thu được phần đó. Ghép với trend nghĩa là vế
B **đệm cho vế A khi trend chao đảo** (các tên pump blow-up khi đảo chiều đúng là cái vế B đang short) →
drawdown dịu hơn nhiều (−6%) và độ bền ngoài mẫu mạnh nhất bộ.

## Pseudocode cho dev

```python
trend = (close - close.rolling(2880).mean()) / close.rolling(2880).std()   # 120d @ 1h
A = trend.sub(trend.mean(1),axis=0).div(trend.std(1),axis=0)               # cs_zscore
sk = returns.rolling(1440).skew()                                          # skew 60d, theo coin
B = (-sk).sub((-sk).mean(1),axis=0).div((-sk).std(1),axis=0)               # cs_zscore của -skew
sig = A + B
w   = cs_scale(cs_winsorize(sig, 3))                                       # trọng số độ lớn
# rebalance 48, shift(1) exec-lag, vol-target 10%
```

## Hiệu năng & vai trò

OS 2.37 > IS 1.66 (**WFE 1.43 — cao nhất**), maxDD −6% (nông nhất cả 10). **|corr| 0.49–0.73 vs trend,
−0.01 vs Amihud** → một diversifier thứ hai thực sự bên cạnh G3. Cùng G3 nó gánh sổ qua các regime đi
ngang/chop nơi sleeve trend thuần chững lại. Thành viên của cả DECORRELATED-5 lẫn low-corr core
(S3+G3+G5 → OS 2.88).

## Caveat

- Vế A dùng chung bet trend, nên G5 chỉ *nửa* độc lập với cụm trend (không trực giao như G3).
- Skew nhiễu trên cửa sổ ngắn; 60d (1440 bar) là mức làm mượt — rút ngắn sẽ thêm turnover và nhiễu.
- Như tất cả ở đây: thiên trend-regime + survivorship/selection → OS là trần lạc quan.

## Liên quan

- [overview](overview.md) · [amihud](amihud.md) (diversifier trực giao) · [trend-z](trend-z.md) (vế A) · [trend-breakout](trend-breakout.md)
- reference: [operators](../reference/operators.md) · [pipeline](../reference/pipeline.md)
