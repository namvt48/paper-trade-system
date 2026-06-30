# Song Than V11 — Combined

Bản **hợp nhất hai chiều** + lớp quản trị rủi ro (mục tiêu **MaxDD ≤ 20%**):

- **SHORT** = breakdown-continuation 15m (logic V7, lấy từ `backtest_v9_regime.gen_signals_v7`)
- **LONG** = trend-following 4h (Donchian-55 + EMA pullback, từ `backtest_v10_trend`) — cố ý độc lập zone
- **Overlay**: R1 sizing theo regime, R4 de-risk khi sụt vốn, cap vị thế, risk thấp

> Đây là bản **ổn nhất** (DD thấp nhất, Calmar cao nhất, dương cả IS lẫn OOS).

## Files (cần đủ cả 3 — v11 import v9 & v10)

| File | Vai trò |
|------|---------|
| `backtest_v11_combined.py` | Điểm chạy chính — gộp tín hiệu + overlay rủi ro |
| `backtest_v9_regime.py` | Cung cấp SHORT (gen_signals_v7), zone logic, `load`, regime |
| `backtest_v10_trend.py` | Cung cấp LONG trend 4h |
| `README.md` | Tài liệu này |

## Yêu cầu

| Thành phần | Phiên bản |
|-----------|-----------|
| Python | 3.10+ (test trên 3.11) |
| Thư viện | `pip install pandas numpy` |

## Chuẩn bị dữ liệu (không kèm trong gói)

Đặt **10 file CSV** vào **cùng thư mục** với các script:

```
BTCUSDT_15m_full.csv  ETHUSDT_15m_full.csv  APTUSDT_15m_full.csv
SANDUSDT_15m_full.csv AVAXUSDT_15m_full.csv XRPUSDT_15m_full.csv
ENSUSDT_15m_full.csv  MAGICUSDT_15m_full.csv CFXUSDT_15m_full.csv
DOGEUSDT_15m_full.csv
```

Cột yêu cầu: `open_time` (ms), `open`, `high`, `low`, `close` — nến **15m**
(khung 4h cho LONG được **tự resample** từ chính data 15m này).

## Chạy

```bash
python3 backtest_v11_combined.py
```

In ra tổng tín hiệu (SHORT + LONG), rồi 4 config × 3 cửa sổ (TOÀN CHU KỲ / IS / OOS):
**Baseline**, **+R1 regime-size**, **+R1+R4 dd-derisk**, **Short-only (tham chiếu)**.

## Tham số chính

| Tham số | Giá trị | Ý nghĩa |
|---------|---------|---------|
| `BASE_RISK` | `0.004` | Rủi ro 0.4% vốn/lệnh |
| `MAX_OPEN` | `5` | Trần vị thế mở đồng thời |
| R1 regime-size | BULL: L×1.0/S×0.5 · BEAR: L×0/S×1.0 · SIDEWAYS: ×0.5 | Sizing theo regime |
| R4 dd-derisk | equity < 0.85·đỉnh → size ×0.5 | Giảm rủi ro khi sụt vốn |
| SHORT engine | V7 (WAIT=4, SL 1.75/2.5%, TP=SL×1.43, hold 64) | Từ v9 |
| LONG engine | 4h Donchian-55 + EMA pullback, ATR chandelier trail, +2R scale-out | Từ v10 |
| `CUT_OOS` | `1735689600000` | Mốc chia IS/OOS (2025-01-01) |

## Kết quả backtest (chạy thực tế, vốn $10k)

| Config | Toàn chu kỳ: ret / MaxDD / PF / Calmar / lệnh |
|--------|-----------------------------------------------|
| Baseline | +290% / -30.5% / 1.12 / 9.50 / 5677 |
| +R1 regime-size | +185% / -22.6% / 1.12 / 8.18 / 5676 |
| **+R1+R4 dd-derisk** | +180% / **-19.6%** / 1.13 / **9.19** / 5676 |
| Short-only (tham chiếu) | +133% / -25.8% / 1.10 / 5.14 / 4664 |

OOS 2025+: +R1+R4 đạt +30% / MaxDD -14.8% / PF 1.12. (Short-only OOS +50% / -12.2% / 1.17.)

## Cảnh báo

- Đòn bẩy **50x**; chiều **LONG vẫn là gánh nặng** — bản Short-only thường tốt hơn ở OOS.
- Edge mỏng + rủi ro overfit (coin chọn sau khi xem kết quả). Paper-trade trước khi vào thật.
- Cần đủ cả 3 file `.py` cùng thư mục; thiếu v9 hoặc v10 sẽ lỗi import.
