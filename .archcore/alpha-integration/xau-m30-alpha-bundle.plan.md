---
title: "Kế hoạch triển khai Alpha XAU M30 theo gate"
status: draft
tags:
  - "alpha"
  - "paper-trading"
  - "runner"
  - "testing"
  - "xauusdt"
---

## Mục tiêu

Tích hợp bộ logic Alpha 4/5/6/10/11/12 vào Paper Trade theo một đường logic duy nhất từ replay đến runtime, nhưng chỉ bật paper execution cho alpha vượt qua gate dữ liệu, chi phí, parity và risk. Ước lượng: 55–75 giờ kỹ thuật, chưa tính thời gian chờ 30 ngày paper observation.

## Công việc

### Giai đoạn 0 — Chốt contract trước khi port (5–7 giờ)

- [ ] 0.1 Chốt mapping `XAUUSD → XAUUSDT`, đơn vị quantity, tick/step/min-notional/leverage từ exchange info — 1 giờ.
- [ ] 0.2 Viết contract thời gian: UTC storage, session `America/New_York`, DST, weekday/holiday và quy tắc M30/H4 as-of — 1,5 giờ.
- [ ] 0.3 Viết contract execution: signal-close → executable fill, fee, slippage, funding, gap và same-bar ordering — 2 giờ.
- [ ] 0.4 Chốt policy early-reversal so với intrabar SL/TP và breakeven có hiệu lực từ nến kế tiếp — 1 giờ.
- [ ] 0.5 Chốt policy cho Alpha 4/10: multi-leg thật, net add-on hoặc giữ disabled; không đổi ngầm semantics — 1 giờ.

**Gate G0:** chưa port runtime cho đến khi các contract trên được duyệt.

### Giai đoạn 1 — Làm baseline nghiên cứu đáng tin (14–18 giờ)

- [ ] 1.1 Lập data manifest cho XAUUSDT từ 2025-12-11 và nguồn XAUUSD dài hạn; ghi checksum, timezone, gap và provenance — 2 giờ.
- [ ] 1.2 Sửa tính toàn vẹn replay harness của bundle (missing imports, dependency manifest, deterministic seed/config) — 2 giờ.
- [ ] 1.3 Tách indicator primitives EMA/RSI/ATR/slope/Donchian/volatility-regime thành hàm thuần có test — 2 giờ.
- [ ] 1.4 Tách evaluator Alpha 4/5/6/10 thành preset trên common pipeline — 2 giờ.
- [ ] 1.5 Tách evaluator Alpha 11/12 và xác nhận mọi rolling level đều shift đúng — 2 giờ.
- [ ] 1.6 Xây event replay dùng M15→M30, completed H4 và executable next quote thay cho same-close fill — 2 giờ.
- [ ] 1.7 Thêm cost model: maker/taker matrix, p50/p95 book slippage, funding 8h và gap stress — 2 giờ.
- [ ] 1.8 Chạy anchored walk-forward, parameter perturbation ±10–20%, bootstrap/Monte Carlo và correlation giữa alpha — 2 giờ.
- [ ] 1.9 Xuất scorecard pass/fail theo PRD, tách kết quả XAUUSD dài hạn và XAUUSDT overlap — 2 giờ.

**Gate G1:** alpha chỉ được vào runtime nếu OOS PF ≥ 1,20 sau chi phí, cost headroom ≥ 1,5x, DD và trade count đạt PRD. Với bằng chứng hiện tại: ưu tiên nghiên cứu Alpha 5/12, kế tiếp 6/11; defer 4 và stop/refine 10.

### Giai đoạn 2 — Adapter vào shared runner (16–20 giờ)

- [ ] 2.1 Tạo module đăng ký strategy `xau_m30` và schema params/preset — 1,5 giờ.
- [ ] 2.2 Đọc M15/H4 từ `SharedCandleCache`, ghép đúng một M30 đã đóng và chống scan trùng — 2 giờ.
- [ ] 2.3 Dùng evaluator thuần từ G1 trong compute path; không sao chép công thức sang runtime — 2 giờ.
- [ ] 2.4 Thêm readiness/warm-up/retain requirements cho M15 và H4, fail-closed khi thiếu/gap/stale — 1,5 giờ.
- [ ] 2.5 Implement entry sizing theo virtual capital và R, round quantity theo contract filters — 2 giờ.
- [ ] 2.6 Phát `OPEN` với TP/SL, fee, exchange, candle ids và metadata parity — 1,5 giờ.
- [ ] 2.7 Implement position state, worker-authoritative reconciliation và restart recovery — 2 giờ.
- [ ] 2.8 Implement `MODIFY/CLOSE` cho BE, TP/SL và early reversal đúng event ordering — 2 giờ.
- [ ] 2.9 Thêm dashboard columns/metrics cho preset, ATR, R, session, decision/fill và lý do loại signal — 1,5 giờ.
- [ ] 2.10 Viết unit/golden tests cho signal parity của sáu preset — 2 giờ.
- [ ] 2.11 Viết integration tests runner→Redis→worker→SQLite và restart/duplicate scenarios — 2 giờ.

Các file dự kiến: @alphas/runner/strategies/xau_m30/, @alphas/runner/strategy/base.py, @alphas/runner/tests/, @runner-config.yaml; tránh tạo sáu container/engine độc lập.

### Giai đoạn 3 — Pyramiding và risk nhóm XAU (8–12 giờ)

- [ ] 3.1 Thiết kế storage key theo `position_id/leg_id` thay vì dict chỉ theo symbol — 2 giờ.
- [ ] 3.2 Chọn và đặc tả signal `ADD/INCREASE` hoặc multi-position; cập nhật idempotency/duplicate policy — 2 giờ.
- [ ] 3.3 Implement hoặc giữ Alpha 4/10 disabled theo quyết định 3.2 — 2 giờ.
- [ ] 3.4 Thêm XAU group admission: per-alpha ≤ 0,25%, aggregate initial risk ≤ 0,75%, gross cap và same-direction cap — 2 giờ.
- [ ] 3.5 Test concurrent opens, partial close, restart reconcile và ownership monitor với nhiều leg — 2 giờ.
- [ ] 3.6 Đánh giá lại Alpha 4/10 sau phí và aggregate risk; chỉ bật nếu qua Gate G1 — 2 giờ.

### Giai đoạn 4 — Execution/PnL fidelity (6–10 giờ)

- [ ] 4.1 Xác nhận XAUUSDT đi qua order-book fill; ghi fallback reason và latency cho mọi OPEN/CLOSE — 1,5 giờ.
- [ ] 4.2 Thêm funding accrual ledger vào paper PnL/equity hoặc đánh dấu rõ metric chưa funding-adjusted — 2 giờ.
- [ ] 4.3 Test fee hai chiều, funding, partial close, gap-through-stop và p95 slippage — 2 giờ.
- [ ] 4.4 Thêm report decision price vs fill price vs backtest expected fill — 1,5 giờ.
- [ ] 4.5 Kiểm tra liquidation distance/margin headroom dù worker không gửi lệnh live — 1 giờ.

### Giai đoạn 5 — Cấu hình và rollout (6–8 giờ + observation)

- [ ] 5.1 Thêm sáu config entry ở trạng thái disabled, chung module/timeframe set; validate dry-run/warm-up — 1 giờ.
- [ ] 5.2 Bật Alpha 5 và 12 trước với 0,15–0,25% risk/alpha; 6 và 11 vẫn disabled — 1 giờ.
- [ ] 5.3 Chạy smoke 24 giờ: candle freshness, scan latency, signal dedup, fills, TP/SL subscriptions và ownership — 2 giờ.
- [ ] 5.4 Bật 6/11 nếu wave 1 ổn; giữ 4/10 disabled đến khi Gate G3 đạt — 1 giờ.
- [ ] 5.5 Theo dõi tối thiểu 30 ngày hoặc 50 trades/alpha; so replay dự kiến với paper fills hàng ngày — 1 giờ setup.
- [ ] 5.6 Viết quyết định promote/refine/stop riêng cho từng alpha dựa trên scorecard — 2 giờ.

## Tiêu chí chấp nhận

- [ ] Một source of truth cho logic alpha; replay và runtime không có bản sao công thức.
- [ ] Golden replay cho từng alpha khớp 100% signal, initial R, TP/SL và exit reason.
- [ ] Không dùng nến chưa đóng, không lookahead H4, không sai session khi DST đổi.
- [ ] Mọi fill có decision price, executable fill, fee và slippage metadata; funding được tính hoặc metric có nhãn loại trừ funding.
- [ ] Alpha bật execution đạt toàn bộ Gate G1; không alpha nào được chọn chỉ vì in-sample PnL.
- [ ] Alpha 4/10 không bị ép thành single-position mà không đổi tên/version và backtest lại.
- [ ] Không duplicate/orphan position qua restart; ownership monitor sạch trong 30 ngày.
- [ ] Tổng risk nhóm XAU không vượt policy và các contract filter đều được round/validate.
- [ ] `make test-runner`, worker test suite và integration replay đều pass; dry-run runner có đủ warm-up M15/H4.

## Phụ thuộc

- Dữ liệu XAUUSD dài hạn đủ sạch và dữ liệu XAUUSDT overlap từ Binance.
- MDS phải cung cấp M15, H4, ticker/price-alert và order book cho `XAUUSDT`; M30 được ghép trong runner để không phải sửa rollup MDS.
- Contract fee/funding và exchange filters phải được snapshot theo thời điểm test.
- Giai đoạn 2 phụ thuộc Gate G1; Giai đoạn 3 phụ thuộc quyết định multi-leg; rollout phụ thuộc G2–G4.

## Rủi ro và giảm thiểu

- Lịch sử XAUUSDT ngắn → dùng XAUUSD dài hạn cho regime robustness và chỉ dùng overlap để kiểm tra transfer/basis.
- Sáu alpha tương quan cao → group risk cap và correlation gate.
- Edge biến mất sau taker fee → không bật; thử execution maker chỉ khi paper model mô phỏng đúng và fill-rate được đo.
- Intrabar ambiguity M30 → replay bằng dữ liệu 1m/5m và policy stop-first rõ ràng.
- 24/7 perp khác XAU cash/CFD → session/weekend policy là tham số được test, không giả định.

## Ngoài phạm vi

- Live order routing.
- Tối ưu tham số trên holdout hoặc thay đổi Alpha để “cứu” kết quả mà không tạo version mới.
- Bật đồng thời cả sáu alpha trước khi từng gate hoàn tất.