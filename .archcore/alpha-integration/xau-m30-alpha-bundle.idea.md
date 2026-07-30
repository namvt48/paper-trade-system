---
title: "Tích hợp bộ Alpha XAU M30 vào Paper Trade"
status: draft
tags:
  - "alpha"
  - "paper-trading"
  - "runner"
  - "xauusdt"
---

## Ý tưởng

Đưa sáu chiến lược XAU M30/H4 (Alpha 4, 5, 6, 10, 11, 12) vào shared alpha runner dưới dạng một họ chiến lược tham số hóa, dùng chung một lớp logic thuần cho replay/backtest và runtime. Đối tượng giao dịch runtime là Binance `XAUUSDT`; dữ liệu vào là nến đã đóng, M30 được ghép xác định từ 15m và bộ lọc H4 chỉ dùng nến H4 đã hoàn thành.

## Giá trị

- Loại bỏ chênh lệch logic giữa bundle nghiên cứu và paper runtime.
- Tận dụng cache, warm-up, lease, reconciliation, Redis signal contract và order-book fill hiện có.
- Cho phép so sánh sáu alpha trên cùng dữ liệu, chi phí và risk budget trước khi bật.
- Tạo lộ trình chọn alpha theo bằng chứng thay vì triển khai đồng loạt.

## Cách triển khai khả dĩ

1. Chuẩn hóa một evaluator thuần cho indicator, entry, stop, target và chuyển trạng thái vị thế.
2. Xây replay harness theo đúng thứ tự sự kiện runtime, timezone New York và dữ liệu Binance XAUUSDT.
3. Đăng ký một runner strategy mới với các preset Alpha 4/5/6/10/11/12; cấu hình từng preset bằng `alpha_id` riêng.
4. Dùng signal contract `OPEN/MODIFY/CLOSE`, fill thực tế từ worker và lưu metadata để kiểm tra parity.
5. Rollout theo gate: Alpha 5/12 trước, Alpha 6/11 sau; Alpha 4/10 chỉ vào runtime sau khi giải quyết semantics pyramiding.

Các điểm tích hợp chính: @alphas/runner/strategy/base.py, @alphas/runner/main.py, @alphas/runner/signal/dispatcher.py, @worker/app/executor.py, @worker/app/db.py và @runner-config.yaml.

## Rủi ro và ràng buộc

- Bundle hiện không tự import được vì `strategies/__init__.py` tham chiếu các file không được bàn giao (`baseline`, Alpha 7/8/9).
- Backtest gốc dùng giá close của nến tín hiệu làm entry, không cố định timezone đầu vào, cho phép tín hiệu cuối tuần và chưa tính funding; kết quả hiện tại chỉ là exploratory.
- Với phí mặc định paper `0.0005` mỗi chiều, cả sáu alpha mất edge trên giai đoạn XAUUSDT khả dụng; cần gate chi phí trước khi bật.
- Alpha 4 và 10 mở nhiều vị thế cùng `alpha_id + symbol`, xung đột với duplicate policy và reconciliation hiện tại.
- Sáu alpha cùng giao dịch một tài sản nên rủi ro danh mục có tương quan rất cao; không được cấp 1% risk độc lập cho từng alpha.
- XAUUSDT chỉ có lịch sử từ 2025-12-11; cần thêm lịch sử XAUUSD và kiểm tra transfer/basis trong giai đoạn overlap.