# Song Than V8 — Dual-Side

Chiến lược **dual-side** trên Futures 15m: từ mỗi cú phá **Green Zone** xuống, bóc ra
hai nhánh loại trừ nhau — **LONG bẫy gấu** (giá reclaim trong ≤3 nến) và
**SHORT continuation** (4 nến không reclaim → trượt tiếp, đúng logic V7).

> Đây là bản **trade nhiều lệnh nhất** trong họ Song Than (~8.6k lệnh IS+OOS, 10 coin).
> Lưu ý: nhánh LONG có PnL âm — toàn bộ lợi nhuận đến từ SHORT.

## Files

| File | Vai trò |
|------|---------|
| `backtest_v8_dualside.py` | Toàn bộ chiến lược + backtest (standalone, không import nội bộ) |
| `README.md` | Tài liệu này |

## Yêu cầu

| Thành phần | Phiên bản |
|-----------|-----------|
| Python | 3.10+ (test trên 3.11) |
| Thư viện | `pip install pandas numpy` |

## Chuẩn bị dữ liệu (không kèm trong gói)

Đặt **10 file CSV** vào **cùng thư mục** với script (script tự tìm theo `DIR`):

```
BTCUSDT_15m_full.csv  ETHUSDT_15m_full.csv  APTUSDT_15m_full.csv
SANDUSDT_15m_full.csv AVAXUSDT_15m_full.csv XRPUSDT_15m_full.csv
ENSUSDT_15m_full.csv  MAGICUSDT_15m_full.csv CFXUSDT_15m_full.csv
DOGEUSDT_15m_full.csv
```

Mỗi CSV cần các cột: `open_time` (ms), `open`, `high`, `low`, `close` — nến **15m**.

## Chạy

```bash
python3 backtest_v8_dualside.py
```

In ra: số tín hiệu LONG/SHORT mỗi coin, rồi so sánh 3 config trên IS (2020-2024)
và OOS (2025→nay): **SHORT-only (V7)**, **LONG-only (bẫy gấu)**, **DUAL**.

## Tham số chính

| Tham số | Giá trị | Ý nghĩa |
|---------|---------|---------|
| `RISK_PCT` | `0.005` | Rủi ro 0.5% tài khoản/lệnh (notional = balance·risk/SL) |
| `MAX_OPEN` | `6` | Trần vị thế mở đồng thời toàn portfolio |
| `WAIT` | `4` | Số nến xác nhận SHORT (không reclaim) |
| `RECLAIM_N` / `RECLAIM_M` | `3` / `0.0015` | LONG khi reclaim trong 3 nến, vượt biên ×1.0015 |
| `SL_CLAMP` | `(0.0175, 0.025)` | Kẹp SL nhánh LONG |
| `TP_RATIO` | `1.43` | TP = SL × 1.43 |
| `HOLD_SHORT` / `HOLD_LONG` | `64` / `32` | Giới hạn giữ lệnh (nến 15m) |
| `LEVERAGE` | `50` | Đòn bẩy |
| `CUT_OOS` | `1735689600000` | Mốc chia IS/OOS (2025-01-01) |
| `COINS` | 10 coin, SL riêng | BTC 1.75%, còn lại 2.5% |

## Kết quả backtest (chạy thực tế, vốn $10k)

| Config | IS: ret / PF / MaxDD / lệnh | OOS: ret / PF / MaxDD / lệnh |
|--------|------------------------------|------------------------------|
| SHORT-only (V7) | +86.3% / 1.07 / -35.0% / 3517 | +75.3% / 1.17 / -17.1% / 1440 |
| LONG-only (bẫy gấu) | -51.9% / 0.92 / -61.1% / 3720 | -40.6% / 0.87 / -45.2% / 1838 |
| **DUAL** | +64.1% / 1.04 / -32.2% / **6147** | +49.3% / 1.08 / -20.7% / **2496** |

## Cảnh báo

- Đòn bẩy **50x** → MaxDD lớn; nhánh **LONG lỗ** ở cả 2 kỳ (chỉ thêm lệnh, không thêm tiền).
- Edge mỏng; slippage thật (~0.05%/lệnh) có thể ăn phần lớn lợi nhuận.
- Danh sách 10 coin được chọn sau khi xem kết quả → rủi ro overfit. Paper-trade trước khi vào thật.
