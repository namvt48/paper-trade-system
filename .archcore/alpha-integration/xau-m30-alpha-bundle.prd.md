---
title: "Yêu cầu tích hợp và đánh giá Alpha XAU M30"
status: draft
tags:
  - "alpha"
  - "paper-trading"
  - "risk"
  - "xauusdt"
---

## Tầm nhìn

Paper Trade có thể chạy, quan sát và so sánh các Alpha XAU M30 bằng cùng contract dữ liệu, execution và risk; logic replay và runtime phải tạo quyết định giống nhau trên cùng chuỗi nến.

## Vấn đề

Bundle nghiên cứu chưa phải artifact production: thiếu module được import, không có test/result manifest, execution backtest lạc quan hơn worker và Alpha 4/10 không tương thích mô hình một vị thế cho mỗi `alpha_id + symbol`.

Khảo sát ngày 2026-07-23 trên 64.473 nến Binance XAUUSDT 5m từ 2025-12-11, resample theo engine gốc sang M30, vốn 100.000 và risk 1% cho kết quả:

| Alpha | PF / DD với 0,15 USD/oz | PF / DD với 5 USD/oz |
|---|---:|---:|
| 4 | 1,25 / 9,38% | 0,74 / 18,22% |
| 5 | 1,58 / 8,85% | 0,91 / 10,95% |
| 6 | 1,40 / 10,08% | 0,86 / 10,90% |
| 10 | 1,04 / 19,85% | 0,63 / 51,34% |
| 11 | 1,41 / 8,64% | 0,90 / 10,94% |
| 12 | 1,46 / 9,47% | 0,94 / 11,95% |

Đây không phải kết luận đầu tư: engine còn bias và 5 USD/oz chỉ xấp xỉ phí taker hai chiều theo cấu hình paper hiện tại.

## Mục tiêu và chỉ số thành công

- Replay và runtime khớp 100% decision/SL/TP trên golden fixtures.
- Không lookahead; timezone/DST và thứ tự intrabar được định nghĩa, kiểm thử.
- Alpha được bật phải có OOS PF ≥ 1,20 sau fee, p95 slippage và funding; cost break-even ≥ 1,5 lần chi phí mô hình.
- OOS có ít nhất 100 trade, max drawdown ≤ 12% tại risk 0,5%/trade và expectancy dương ở ≥ 60% rolling quarter.
- 30 ngày paper hoặc tối thiểu 50 trade không có orphan/duplicate position, missed candle hay signal trùng.
- Tổng initial risk của nhóm XAU ≤ 0,75% equity; mỗi alpha khởi đầu ≤ 0,25%.

## Yêu cầu

### P0

- Dùng `XAUUSDT`, exchange `binance`, quantity theo ounce; round tick `0.01`, step `0.001`, min notional `5`, leverage không quá `10x`.
- Ghép M30 từ hai nến 15m đã đóng; H4 phải là nến hoàn thành as-of thời điểm quyết định.
- Session dùng `America/New_York`, xử lý DST và chặn tín hiệu cuối tuần theo policy được kiểm chứng.
- Logic indicator/signal chỉ có một implementation dùng chung replay và runtime.
- Entry dùng executable quote sau close; fee hai chiều, book slippage và funding phải có trong đánh giá.
- TP/SL, breakeven 0,8R, early reversal và same-bar ordering có contract rõ ràng.
- Runtime phục hồi vị thế từ worker snapshot và phát signal idempotent.

### P1

- Metadata/dashboard phải hiển thị preset, session, R, ATR, regime, decision price, fill price và exit reason.
- Báo cáo walk-forward, cost stress, parameter perturbation, correlation và contribution của từng alpha.
- Alpha 5/12 là wave đầu; Alpha 6/11 là wave sau nếu qua gate.

### P2

- Alpha 4/10 chỉ bật khi worker/runner hỗ trợ nhiều leg hoặc net-position add-on đúng semantics; không tự động ép `max_active_trades = 1`.
- Funding ledger và portfolio-level XAU admission có thể tái sử dụng cho các TradFi perp khác.

## Ngoài phạm vi

- Gửi lệnh live lên Binance.
- Tối ưu tham số trên holdout.
- Cam kết profitability dựa trên giai đoạn dữ liệu ngắn hiện có.