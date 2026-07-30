# PLAN — P0: Reconciliation ledger + đếm đường drop signal ("no silent failures")

STATUS: APPROVED
Owner: Claude (Architect)
Created: 2026-07-17
Approved: 2026-07-17 (đại diện chốt 3 câu — xem "Resolved Decisions")
Scope: `alphas/runner/signal/`, `alphas/runner/metrics.py`, `worker/app/`, `scripts/` (thêm reconcile). KHÔNG đổi công thức tín hiệu, KHÔNG đổi cơ chế commit (batching là P1, ngoài phạm vi).
Deploy: **KHÔNG deploy lên server trong plan này** (đại diện chốt). Chỉ implement + test local. Deploy tách ra quyết định riêng sau khi review số liệu.

---

## Summary

Mục tiêu P0: biến câu "giảm signal tốt hay xấu" thành **một invariant kiểm chứng được**, để mọi signal biến mất đều có lý do ghi nhận được — không rơi âm thầm. Hiện tại có 2 đường drop signal **không đếm được**: (1) runner drop khi lease invalid (`dispatcher.py:32-42`) = **mất lệnh rebalance thật**; (2) worker drop khi parse/process lỗi (`main.py`) = signal nhận nhưng không thành trade. Cả hai chỉ có log rời rạc, không có counter, không có cách đối chiếu tổng.

Plan thêm counter ở cả hai đầu + một reconcile script chạy độc lập (đọc Redis stream + SQLite) in ra chuỗi invariant và **exit nonzero khi có gap không giải thích được**. Đây chính là "đối chiếu số signal lọc với trade/open/equity" mà phân tích trước yêu cầu, giờ thành công cụ tự động.

Không đụng throughput/batching. Không deploy server.

## Problem Frame

Luồng signal: `alpha runner → SignalDispatcher.dispatch() → XADD stream → worker XREADGROUP → SQLite (signals/trades/positions) → XACK`.

Các đường signal biến mất, và tình trạng quan sát hiện tại:

| Đường | Vị trí code | Bản chất | Đếm được? |
|---|---|---|---|
| Lease invalid → drop | `dispatcher.py:32-42` | **XẤU** — lệnh rebalance bị bỏ | ❌ chỉ log WARNING |
| Dedup theo signal_id | `dispatcher.py:43-52` | Lành tính (idempotent theo nến) | ❌ chỉ log INFO |
| XADD published | `dispatcher.py:66` | Hợp lệ | ❌ không counter |
| Worker duplicate skip | `main.py:121` | Lành tính | ❌ chỉ log WARNING |
| Worker parse error | `main.py:126-133` | **XẤU** — signal hỏng | ❌ ghi `signals.error`, không counter |
| Worker process error | `main.py:170-173` | **XẤU** — không thành trade | ❌ ghi `signals.error`, không counter |

Hạ tầng sẵn có:
- Runner: `RunnerMetrics` (`metrics.py`) + endpoint `/metrics`, `/health` (`metrics_http.py`) — dễ mở rộng.
- Worker: **KHÔNG có** module metrics, **KHÔNG có** HTTP endpoint — chỉ structured logging (`logging_config.py`).
- SQLite `signals` table có cột `processed`, `error` → truy được sự thật sau-commit. `trades`, `positions` cho số trade/open.
- Redis stream + consumer group → `XLEN`, `XINFO GROUPS` (entries-read, lag), `XPENDING`.

## Requirements

- R1: Mọi đường drop signal phải có counter tăng đơn điệu, phân biệt **drop lành tính** (dedup/duplicate) và **drop nguy hiểm** (lease/parse/process).
- R2: Có một phép đối chiếu tổng (reconciliation) chứng minh: `dispatched = dedup + lease_dropped + published`, `published ≈ received + in-flight`, `received = duplicate + parse_err + committed + process_err`, `committed = opens + closes + modifies + register`.
- R3: Gap không giải thích được (không do in-flight/pending) phải làm reconcile **fail loud** (exit nonzero + log ERROR), không nuốt.
- R4: Không đổi công thức tín hiệu, không đổi cơ chế commit, không đổi ngữ nghĩa dedup (numeric/behaviour parity).
- R5: Blast radius tối thiểu ở worker — không mở HTTP port mới nếu tránh được (worker là service đơn mục đích).
- R6: Test-first cho mỗi counter và cho reconcile invariant (RED trước GREEN).
- R7: KHÔNG deploy lên server; verify chỉ bằng `make test` local + chạy reconcile trên DB/stream local hoặc bản copy.

## Key Decisions

- KD1: Truyền `RunnerMetrics` vào `SignalDispatcher` (hiện construct ở `main.py:596` không có metrics) và thêm 4 counter: `signals_dispatched_total`, `signals_dedup_skipped_total`, `signals_lease_dropped_total`, `signals_xadd_published_total` — quyết định: tái dùng hạ tầng `/metrics` sẵn có thay vì cơ chế mới; `metrics=None` mặc định để test cũ và callsite chưa truyền không vỡ (no-op).
- KD2: Worker thêm module `metrics.py` in-process nhẹ (`WorkerMetrics`, cùng pattern `inc(name)` như runner) + **một dòng log reconciliation định kỳ** (structured, mỗi `RECONCILE_LOG_INTERVAL_SEC`), KHÔNG mở HTTP port — quyết định: thỏa R5, worker chỉ cần snapshot ra log để đối chiếu, không cần scrape realtime.
- KD3: Nguồn sự thật cho reconcile là **SQLite + Redis trực tiếp**, không phụ thuộc counter in-process của worker — quyết định: counter in-process reset khi restart, còn DB/stream là bền; reconcile script phải đúng kể cả sau restart. Counter in-process chỉ để quan sát live, DB/stream để audit.
- KD4: Reconcile là **script độc lập** `scripts/reconcile_signals.py` (chạy tay hoặc cron), không nhúng vào hot path worker — quyết định: giữ hot path sạch (đúng tinh thần P1 chưa đụng), audit là tác vụ ngoài luồng.
- KD5: Ngưỡng "gap không giải thích được" = `published - received - pending_lag`; nếu > `RECONCILE_TOLERANCE` (mặc định 0) sau khi trừ in-flight → FAIL. Dedup hai đầu được cộng lại, không tính là mất — quyết định: phân biệt rạch ròi "giảm hợp lệ" vs "mất".
- KD6: Alert đường nguy hiểm (lease_dropped, parse_err, process_err) — trong P0 chỉ nâng log lên mức ERROR + expose counter; KHÔNG làm kênh alert ngoài (Slack/webhook) — quyết định: kênh alert là việc riêng, P0 chỉ đảm bảo "quan sát được".

## Implementation Units

### U1 — Counter đường drop phía runner (R1, KD1) — test-first
- `alphas/runner/metrics.py`: thêm 4 field counter + đã có `.inc(name)`.
- `alphas/runner/signal/dispatcher.py`: `__init__(..., metrics=None)`; tăng `signals_dispatched_total` đầu `dispatch()`, `signals_lease_dropped_total` ở nhánh lease invalid, `signals_dedup_skipped_total` ở nhánh dedup, `signals_xadd_published_total` sau `xadd`. Dùng `if self.metrics is not None`.
- `alphas/runner/main.py:596`: truyền `metrics=` vào constructor.
- Nâng log lease-drop (`dispatcher.py:34`) từ WARNING → ERROR (KD6 — đây là mất lệnh thật).
- Acceptance: test mới `test_dispatcher_counters.py` — (a) dispatch thành công tăng dispatched+published; (b) lease invalid tăng dispatched+lease_dropped, KHÔNG published; (c) duplicate tăng dispatched+dedup_skipped, KHÔNG published; (d) invariant `dispatched == dedup + lease_dropped + published` sau chuỗi hỗn hợp. `metrics=None` → không lỗi. Test cũ dispatcher GREEN.

### U2 — Counter + reconcile log phía worker (R1, KD2, KD3) — test-first
- `worker/app/metrics.py` (mới): `WorkerMetrics` với `received_total`, `duplicate_skipped_total`, `parse_error_total`, `process_error_total`, `committed_by_type: dict[str,int]`, `xack_total`; `.inc(name)`, `.inc_committed(type)`, `.snapshot()`.
- `worker/app/main.py`: khởi tạo 1 `WorkerMetrics`; tăng counter tại các điểm tương ứng trong `process_signal_message` (received đầu hàm, duplicate ở `main.py:121`, parse_error ở nhánh except parse, process_error ở nhánh except process, committed theo `signal.type`) và `xack_total` sau `xack` (`main.py:488`).
- Nâng log parse_error/process_error lên ERROR (đã sẵn ERROR ở process; xác nhận parse cũng ERROR).
- Thêm task định kỳ `run_reconcile_log_loop` log 1 dòng structured `[RECONCILE] ...` mỗi `RECONCILE_LOG_INTERVAL_SEC` (config, mặc định 300s) với snapshot + `received == duplicate + parse_err + committed_sum + process_err` (invariant nội bộ worker).
- Acceptance: test mới `test_worker_metrics.py` + bổ sung `test_process_signal_message`: mỗi nhánh tăng đúng counter; invariant worker giữ sau chuỗi hỗn hợp (open/close/dup/parse-fail/process-fail). Test cũ worker GREEN.

### U3 — Reconcile script độc lập (R2, R3, KD4, KD5) — test-first
- `scripts/reconcile_signals.py`: input = đường DB (`data/paper-trade.db`), Redis URL, stream, group, cửa sổ thời gian (mặc định toàn bộ). Thu thập:
  - Redis: `XLEN(stream)`, `XINFO GROUPS` (entries-read, lag), `XPENDING` (in-flight).
  - SQLite: `count(*) signals`, `count WHERE error IS NOT NULL`, `count WHERE processed=1 AND error IS NULL`, `count(*) trades`, `count(*) positions WHERE ...opened trong cửa sổ`.
  - (Tùy chọn) runner `/metrics` nếu URL được cấp → lấy `signals_*_total` để đối chiếu producer.
- In chuỗi invariant + phần "giải thích được" (dedup, in-flight/pending) vs "gap". Exit `0` nếu gap ≤ `RECONCILE_TOLERANCE`, exit `1` + log ERROR nếu vượt.
- Acceptance: test mới `scripts/tests/test_reconcile_signals.py` với fake Redis + SQLite tạm: (a) luồng khớp → exit 0; (b) chèn gap nhân tạo (published > received + pending) → exit 1 + thông điệp nêu đúng số gap; (c) in-flight/pending không bị tính là mất → exit 0.

### U4 — Verify local, KHÔNG deploy (R7)
- `make test-runner` + suite worker + `pytest scripts/tests/test_reconcile_signals.py` — tất cả GREEN; xác nhận không vỡ baseline (git stash trước/sau nếu nghi ngờ pre-existing fail).
- Chạy `scripts/reconcile_signals.py` trên **DB + stream local** (hoặc bản copy `data/paper-trade.db`) một lần, đính output vào `.omo/evidence/` hoặc HANDOFF.
- **KHÔNG** `make package`/scp/deploy. Cập nhật `.agents/HANDOFF.md`: đã implement P0, chờ đại diện quyết định deploy.
- Acceptance: log test GREEN + một lần chạy reconcile thật (exit code + output) làm bằng chứng.

## Implementation Status (2026-07-17)

- U1 — HOÀN THÀNH. `metrics.py` +counter, `dispatcher.py` wired (lease-drop → ERROR), `main.py:596` truyền metrics. Test `test_dispatcher_counters.py` (5 case gồm invariant + metrics=None) GREEN; `test_lease_dispatcher` không vỡ.
- U2 — HOÀN THÀNH. `worker/app/metrics.py` (WorkerMetrics), wiring mọi nhánh `process_signal_message` + `xack_total` + `run_reconcile_log_loop`, config `RECONCILE_LOG_INTERVAL_SEC`. Test `test_worker_metrics.py` (7 case) GREEN; worker full suite 199 passed/6 skipped.
- U3 — HOÀN THÀNH. `scripts/reconcile_signals.py` (read-only, exit 1 khi gap>tolerance). Test `test_reconcile_signals.py` (8 case: ok/gap/pending-not-loss/tolerance/db-since/stream/main-exit) GREEN.
- U4 — HOÀN THÀNH (local, KHÔNG deploy). Suites GREEN (2 lỗi scripts pre-existing = thiếu `typer`, không do P0). Reconcile chạy thật trên `data/paper-trade.db`: DB-side invariant khớp (1319 = 1110 committed + 209 errored); Redis local down → fail-loud exit 1 (đúng). **Phát hiện: 209/1319 signal (~16%) đã errored trong DB live** — cần điều tra. Evidence: `.omo/evidence/worker-redis-u2/task-p0-summary.txt`, `task-p0-reconcile-run.txt`.

## Risks

- Counter thêm vào hot path worker phải là phép `dict`/`int` thuần (không I/O) — nếu không sẽ tự tạo overhead ngược P1. Giữ `inc()` thuần bộ nhớ.
- Reconcile đọc SQLite trong lúc worker đang ghi (WAL) — dùng kết nối read-only, chấp nhận ảnh chụp gần-đúng; cửa sổ thời gian nên chốt mép để tránh đếm signal đang in-flight lẫn vào "mất".
- `signals.signal_id` có thể trùng (worker dedup bằng nó) — reconcile đếm theo `signal_id` distinct hay theo row cần thống nhất; chọn **distinct signal_id** cho tầng "đã nhận logic", row cho tầng "ghi vật lý".

## Rollout

1. Implement U1–U3 (test-first) sau khi plan APPROVED. 2. U4 verify local. 3. Dừng lại — **không deploy**; báo cáo số liệu reconcile local cho đại diện. 4. Quyết định deploy (và có gộp P1 batching không) là bước riêng sau review.

---

## Resolved Decisions (2026-07-17)

1. Reconcile **chạy tay** (script chạy tay). Cron/alert = việc riêng sau. ✓
2. Cửa sổ đối chiếu: tham số `--since`, **mặc định toàn bộ** lịch sử. ✓
3. **Có** tách theo `alpha_id`: counter drop/committed giữ cả tổng VÀ dict `..._by_alpha` (như `scan_timeout_by_alpha`). Cụ thể — runner: `signals_lease_dropped_by_alpha`, `signals_dedup_skipped_by_alpha`; worker: `committed_by_type` (đã có) + `parse_error_by_alpha`, `process_error_by_alpha`; reconcile in bảng per-alpha để soi alpha nào drop nhiều. ✓

Unresolved Questions:
- (không còn)
