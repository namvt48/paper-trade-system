# PLAN — Tối ưu hiệu năng & scale-readiness alpha-runner (tách riêng khỏi incident-fix)

STATUS: APPROVED (revised sau review 2026-07-17; đại diện duyệt implement theo plan, U1 đo trước)
Owner: Claude (Architect)
Created: 2026-07-17
Related: `.agents/PLAN.md` (APPROVED, incident-fix U1-U7) — plan này KHÔNG sửa lại các quyết định đó, chỉ tối ưu/mở rộng trên nền đã ổn định.
Scope: chủ yếu `alphas/runner/` (điều phối/compute pool) + đo đạc tầng `worker/` (throughput, chỉ đo — không tối ưu ở plan này trừ khi số liệu buộc phải). KHÔNG đụng logic tín hiệu (`alphas/cross_alpha/strategy.py` phần công thức).

---

## Summary

Plan trước đóng lỗi treo im lặng (reliability), KHÔNG phải tối ưu hiệu năng. Plan này có 3 nhánh (track):
- **Track A — SCALE-READINESS** (phục vụ mục tiêu dài hạn 100-150 alpha): topology 1-runner → nhiều runner, và — quan trọng nhất — **đo/kiểm chứng tầng worker + SQLite** vì đó nhiều khả năng là nút thắt thật ở scale đó.
- **Track B — MICRO-OPT trong-process**: semaphore/cache-hit, `compute_workers`, debounce.
- **Track C — ARCHITECTURE** (đòn bẩy compute lớn nhất, đổi cơ chế): indicator incremental, union-panel, engine — giải quyết bản chất chi phí compute, không chỉ điều phối.

**Điểm mấu chốt:** mọi "chỗ chưa tối ưu" bên dưới là **GIẢ THUYẾT cần đo**, không phải sự thật đã chứng minh — hiện KHÔNG có bằng chứng 27 alpha compute-bound ở steady-state. Rủi ro lớn nhất là **premature optimization**; cấu trúc plan cố tình chống lại điều đó.

**Cổng hiệu-quả (effectiveness gate) — BẮT BUỘC cho mọi unit Track B/C:** trước khi implement 1 tối ưu, dùng số liệu U1/U2 chứng minh **chi phí mà nó nhắm tới là đáng kể thật**. Nếu số liệu cho thấy chi phí nhỏ (vd compute chỉ chiếm 5% thời gian, hoặc nút thắt ở worker/DB chứ không phải runner) → **bỏ qua unit, ghi rõ lý do**. Không xây thứ không đo được lợi ích. Đây chính là "đo xem implement có hiệu quả không TRƯỚC khi implement".

**Mục tiêu 100-150 alpha là "sau này" (chưa có mốc)** — Track A ưu tiên "kiểm chứng ranh giới + chuẩn bị đường scale", không "tối ưu gấp".

## Problem Frame

- Kiến trúc luồng tín hiệu (đã verify code, không suy đoán):
  - Runner **không ghi DB**: `signal/dispatcher.py` chỉ `xadd` signal vào Redis stream `paper-signals`.
  - **Một worker duy nhất** consume stream → ghi `paper-trade.db` (`worker/app/db.py`: 1 connection, `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`). Equity snapshot ghi DB riêng (`equity_snapshots.py`) cũng single-writer.
  - ⇒ Ở scale lớn, **worker + single-writer SQLite là ứng viên nút thắt số 1**, không phải runner compute pool (runner chỉ đẩy vào Redis rồi thôi).
- Số liệu hiện có (production `/metrics` sau incident-fix): 27 alpha, `panel_build_total=5` (5 nhóm build), `panel_build_duration_sec_total≈10.7s` — **chỉ là số lúc warmup, chưa có steady-state**.
- Các điểm NGHI chưa tối ưu (giả thuyết, cần U1 xác nhận):
  1. Semaphore ở `run_strategy_event_loop` bọc **toàn bộ** `handle_strategy_event` kể cả nhánh cache-hit rẻ tiền (không cần thread) → cache-hit vẫn phải xin vé.
  2. `RUNNER_COMPUTE_WORKERS=8` chọn "= số core" (lý thuyết), chưa đo.
  3. Chỉ có `stale_alphas` (phát hiện im lặng lâu), thiếu tín hiệu "pool đang chớm nghẽn".
  4. Debounce `symbols:binance` descope ở incident-fix vì chưa có bằng chứng publish lặp dồn dập.
- Điểm topology (fact, không phải giả thuyết):
  5. `docker-compose.yml` chỉ 1 service `alpha-runner`, không replica. 27/27 alpha trong 1 process / 1 pool. `max_alphas_per_runner=40` & `make runner-scale` có sẵn nhưng chưa dùng.
  6. **Đính chính hiểu lầm ở bản plan trước:** `group_alphas_by_tf_set` ĐÃ tách alpha theo tf_set — daily (`tf={1d}`, funding-heavy) vốn là 1 group claim riêng, tách khỏi 15m/1h. Vấn đề KHÔNG phải "grouping ngẫu nhiên" mà là **claim-affinity**: 1 runner có thể claim CẢ group daily nặng LẪN group high-freq → không cô lập rủi ro. Đây là bài toán ràng buộc claim, không phải thêm grouping.
  7. **Warmup/tải MDS chưa tính khi scale-out:** mỗi runner warmup độc lập với `max_concurrent_mds_requests=6` / `max_mds_requests_per_minute=60` RIÊNG. N runner → tải MDS có thể ×N hoặc tranh chấp; ở 27 alpha warmup đã ~2.5 phút / 788 key.
- Gap KIẾN TRÚC compute (verify code 2026-07-17, đòn bẩy lớn nhất mà bản plan trước bỏ sót hoàn toàn):
  8. **Indicator tính lại từ đầu mỗi nến (không incremental).** `indicators/pandas/ts_ops.py`: `ts_ema = x.ewm(span).mean()`, `ts_zscore = rolling mean/std` — chạy trên **toàn bộ cột lịch sử mỗi lần gọi** (có alpha `max_bars=8640`). `CrossAlphaComputeContext` (chứa `feature_cache`) tạo mới mỗi bundle = mỗi nến → **cache vứt đi, zero tái dùng theo thời gian**. Mỗi 15 phút, mỗi alpha 15m tính lại EMA/z-score trên tới 8640 bars × ~180 symbol dù chỉ 1 bar mới. Single-flight (incident-fix) chỉ gộp trùng GIỮA alpha, KHÔNG giảm chi phí MỖI scan. → Đây là chi phí compute per-scan lớn nhất, scale kém nhất theo (bars × symbols × alpha).
  9. **Không chia sẻ panel giữa universe chồng lấn.** Single-flight key theo `(tf, universe_hash, bars, version)` — chỉ gộp khi universe GIỐNG HỆT. Ở 100-150 alpha, universe chồng lấn ~90% nhưng khác hash → build panel riêng hoàn toàn.
  10. **Trần GIL trên `compute_workers`.** Compute là pandas CPU-bound chạy trong thread; GIL khiến N thread KHÔNG cho N× throughput. Tinh chỉnh số thread (Track B) có thể đụng tường GIL — song song CPU thật chỉ đến từ **process-level** (topology Track A, hoặc `ProcessPoolExecutor`). ⇒ Track B `compute_workers` và Track A topology KHÔNG độc lập: nếu đã bound GIL, lời giải là thêm process (Track A) chứ không phải thêm thread (Track B).
  11. **Warmup fetch trùng lặp giữa runner.** N runner universe chồng lấn → fetch trùng cùng data từ MDS. Đã có parquet cache (`parquet_restore`) — có thể làm nguồn warmup chia sẻ để dedupe.

## Requirements

- R1: Không đổi kết quả tín hiệu/hành vi rebalance (parity — kế thừa plan trước).
- R2: Mọi thay đổi có số đo trước/sau — không tối ưu theo cảm tính/lý thuyết thuần.
- R3: Không làm yếu cơ chế an toàn plan trước (watchdog, single-flight, socket timeout) — chỉ cộng thêm.
- R4: Blast radius nhỏ, rollback độc lập từng unit — không bundle nhiều thay đổi vào 1 deploy.
- R5: Kiểm chứng chịu tải 100-150 alpha phải bao gồm **cả tầng worker+DB**, không chỉ runner — nếu chỉ test runner thì kết luận "chịu được" là sai/thiếu.
- R6: Không viết lại Strategy base/spec/signal framework ("alpha base operator") — chỉ đổi tầng điều phối/triển khai.

## Key Decisions

- KD1: Đo trước, đổi sau. Micro-opt (semaphore/compute_workers/debounce) bị **gate cứng** vào số liệu U1 — không làm nếu U1 không cho thấy vấn đề thật.
- KD2: Semaphore — thay vì THÊM tầng chặn quanh `to_thread` (sẽ **trùng lặp** với `ThreadPoolExecutor(max_workers=8)` đã bound sẵn qua `set_default_executor`), sửa theo hướng **cache-hit KHÔNG acquire semaphore**. Cân nhắc cả phương án bỏ hẳn semaphore thô và để executor + watchdog làm việc chặn (đo ở U1 xem semaphore có còn giá trị riêng ngoài executor không).
- KD3: `queue_depth`/mức nghẽn đo qua counter tự quản (inc khi chờ vé, dec khi vào), KHÔNG dùng `_work_queue.qsize()` (API nội bộ) — giữ nguyên tinh thần incident-fix U6.
- KD4: Debounce CHỈ làm nếu U1 xác nhận publish lặp dồn dập là pattern thật.
- KD5: `compute_workers` chọn theo đo thực nghiệm (thử N giá trị dưới tải mô phỏng, so p50/p95/p99 `scan_ms`) — pandas/numpy release GIL không đều, "=core" không chắc tối ưu.
- KD6: Scale-out dùng CƠ CHẾ ĐÃ CÓ (`claim_alpha_groups`/Redis lease/`runner-scale`), KHÔNG viết claim/distribution mới.
- KD7: Cô lập rủi ro bằng **claim-affinity** (ràng buộc 1 runner chỉ nhận nhóm cùng profile, vd runner riêng cho nhóm daily/funding-heavy) — tận dụng tf-grouping sẵn có, chỉ thêm ràng buộc "runner nào được claim group nào", KHÔNG thêm tầng grouping mới. CHỈ làm nếu số đo cho thấy trộn nhóm rủi ro trong 1 runner thực sự gây vấn đề.
- KD8: Tầng worker+SQLite được **đo và đánh giá ngưỡng** ở plan này (R5), nhưng việc *tối ưu* nó (vd tách DB, batch write, đổi store) — nếu số liệu cho thấy cần — sẽ là plan riêng, không nhồi vào đây (giữ scope sạch).
- KD9 (Track C — indicator incremental): nếu U1 cho thấy indicator-compute là phần lớn `scan_ms`, chuyển EMA/z-score sang **stateful/incremental** (cập nhật O(1)/bar) HOẶC persist compute-context append-only qua các nến thay vì dựng lại. Ràng buộc: phải parity bit-for-bit với engine cũ (R1) — verify bằng so sánh output trên cùng input trước/sau. Đây đụng `indicators/pandas/*` (nhiều nơi dùng) → rủi ro parity cao, làm sau khi U1 xác nhận đáng.
- KD10 (Track C — union panel): thay N panel-build cho N universe chồng lấn bằng 1 **master panel hợp nhất theo tf** + per-alpha view/mask. Chỉ làm nếu U1 cho thấy panel-build (không phải indicator) là chi phí đáng kể VÀ có nhiều universe chồng lấn thật.
- KD11 (engine polars/numpy-native): ghi nhận là option DÀI HẠN — nhả GIL tốt hơn (giải KD10-adjacent + trần GIL #10) nhưng là viết lại lib indicator, rủi ro parity rất cao. KHÔNG làm trong plan này; chỉ mở nếu KD9 không đủ và số liệu buộc phải.

## Implementation Units

> Thứ tự: **U1 (đo, luôn trước)** → cổng hiệu-quả quyết định track nào thực sự làm → Track A (U2 worker/DB, U3 topology — mục tiêu 100-150) ‖ Track B (U4/U5/U6 micro-opt) ‖ Track C (U8 incremental indicators, U9 union panel — đòn bẩy compute) → U7 verify. U2 nên chạy sớm vì kết luận của nó (nút thắt ở worker hay runner) định lại ưu tiên B/C.

### U1 — Baseline profiling + metrics mở rộng (LÀM TRƯỚC, non-invasive; KD1/KD3) — đây là "đo hiệu quả trước khi implement"
- Runner: **breakdown `scan_ms` thành 3 phần: panel-build / indicator-compute / select_positions** (quyết định Track C có đáng không — nếu indicator-compute nhỏ thì KD9 vô nghĩa); `panel_build_duration_sec` theo `(tf, universe_hash)`; tách thời gian chờ semaphore khỏi thời gian thực thi (quyết định U4); counter tần suất `symbols:binance` (quyết định U6); `queue_depth` proxy (KD3, quyết định U5).
- Worker/DB (gap #1): signal-lag (runner `xadd` → worker ghi xong), tốc độ ghi DB, số lần `busy_timeout`, backlog stream `paper-signals`. Quyết định nút thắt thật ở scale.
- Đo mức chồng lấn universe giữa alpha (quyết định U9/KD10): bao nhiêu % symbol dùng chung giữa các universe_hash khác nhau.
- Chỉ THÊM đo đạc, **không đổi hành vi**. Intraday có số liệu trong vài giờ; **daily cần 24-48h** — không để daily-baseline chặn unit chỉ cần số liệu intraday.
- Acceptance: có số liệu thật cho từng quyết định gate ở trên → điền được bảng "unit nào đáng làm, unit nào bỏ".

### U2 — Đánh giá throughput worker + SQLite ở scale (SCALE-READINESS; R5/KD8) — QUAN TRỌNG NHẤT cho mục tiêu 100-150
- Dùng số liệu U1 + benchmark mô phỏng: bơm signal ở tốc độ tương ứng 100-150 alpha vào stream, đo worker có theo kịp không, SQLite single-writer có phải nút thắt không (busy_timeout, WAL checkpoint stall, backlog tăng không kiểm soát).
- Sản phẩm: xác định **ngưỡng vỡ** (bao nhiêu signal/giây thì worker/DB bắt đầu tụt lại) và khoảng cách tới mức 100-150 alpha dự kiến.
- Acceptance: có con số ngưỡng cụ thể + kết luận "worker/DB có phải nút thắt trước runner không". Nếu CÓ → mở plan riêng tối ưu tầng đó (KD8), và hạ ưu tiên nhánh micro-opt runner (vì tối ưu runner không giải quyết nút thắt thật).

### U3 — Runner topology / scale-out (SCALE-READINESS; R5/R6, KD6/KD7)
- Vận hành, không code mới (KD6): bật multi-replica qua `make runner-scale N=<n>`; chọn N theo số đo U1 (không mặc định `max_alphas_per_runner=40`).
- Claim-affinity (KD7, cần thiết kế nhỏ — CHỈ nếu U1 cho thấy trộn nhóm rủi ro gây vấn đề): thêm ràng buộc runner chỉ claim group cùng profile (vd 1 runner chuyên daily/funding-heavy, tách khỏi high-freq), tận dụng tf-grouping sẵn có.
- Warmup/MDS scale (finding #7): xác nhận N runner warmup đồng thời không vượt rate-limit MDS (`max_concurrent_mds_requests`/`max_mds_requests_per_minute` là per-runner) — nếu có, cần điều phối/giãn warmup giữa các runner.
- Test ở scale mô phỏng 100-150 alpha (config nhiều alpha giả với spec thật), không chờ có thật.
- Acceptance: khuyến nghị N cụ thể kèm số đo; xác nhận warmup không hạ gục MDS; (nếu làm affinity) test claim đúng ràng buộc.

### U4 — Semaphore: cache-hit bypass (MICRO-OPT; R1/R3, KD2) — CHỈ nếu U1 cho thấy cache-hit bị chờ đáng kể
- Cache-hit path KHÔNG acquire semaphore; cân nhắc bỏ hẳn semaphore thô nếu U1 cho thấy executor+watchdog đã đủ (semaphore trùng executor).
- Test: cache-hit scan có thời gian chờ ≈0 dù N build khác đang chiếm vé/executor. Test concurrency (incident-fix U4/U5) vẫn xanh.
- Acceptance: test mới xanh; không làm yếu chặn thundering-herd (đo bằng benchmark refresh đồng thời).

### U5 — Tinh chỉnh `compute_workers` (MICRO-OPT; KD5) — CHỈ nếu U1 cho thấy pool là nút thắt VÀ chưa bound GIL
- Thử N giá trị dưới tải mô phỏng mass-refresh, so `scan_ms` p50/p95/p99, chọn theo số.
- **Nối với U3 (gap #10):** nếu tăng thread không cải thiện p95 (dấu hiệu bound GIL), DỪNG tinh chỉnh thread — chuyển sang thêm process (U3 topology). U5 và U3 không độc lập: U5 chỉ có ý nghĩa tới trần GIL, quá đó phải là U3.
- Acceptance: bảng so sánh kèm số đo; hoặc kết luận "đã bound GIL, chuyển sang U3".

### U6 — Debounce `symbols:binance` (MICRO-OPT có điều kiện; KD4) — CHỈ nếu U1 xác nhận publish lặp dồn dập
- Nếu không: đóng unit với ghi chú "không cần" (minh bạch như incident-fix descope).

### U8 — Indicator incremental (TRACK C; KD9) — CHỈ nếu U1 cho thấy indicator-compute là phần lớn `scan_ms`
- Chuyển EMA/z-score sang stateful/incremental (O(1)/bar) hoặc persist compute-context append-only qua các nến thay vì dựng lại toàn history.
- **Bắt buộc parity (R1):** so output bit-for-bit (hoặc trong dung sai float xác định) với engine cũ trên cùng input, trước/sau — đây là rào chắn chính vì đụng `indicators/pandas/*` nhiều nơi dùng.
- Acceptance: parity PASS + `scan_ms` indicator-compute giảm rõ theo số đo.

### U9 — Union panel giữa universe chồng lấn (TRACK C; KD10) — CHỈ nếu U1 cho thấy panel-build đáng kể + chồng lấn cao
- 1 master panel hợp nhất theo tf + per-alpha view/mask thay N build riêng.
- Acceptance: số build giảm theo số đo; parity PASS.

### U7 — Verify + so sánh trước/sau
- Benchmark có kiểm soát (refresh đồng thời, cả quy mô 27 hiện tại lẫn mô phỏng 100-150), so metrics trước/sau mỗi unit.
- Test incident-fix phải xanh — đảm bảo R1/R3.

## Risks

- **Premature optimization** (rủi ro lớn nhất): U4/U5/U6/U8/U9 có thể vô nghĩa nếu nút thắt thật ở worker/DB. Chống lại bằng cổng hiệu-quả (gate cứng vào U1/U2) — không unit nào được implement khi số liệu chưa chứng minh chi phí nó nhắm tới là đáng kể.
- U8 (indicator incremental) rủi ro parity cao nhất — đụng lib nhiều nơi dùng; bắt buộc test parity bit-for-bit trước deploy. Đây là lý do U8 xếp sau, chỉ làm khi U1 chứng minh đáng đánh đổi rủi ro.
- U4 dời/bỏ semaphore có thể lộ race/làm yếu chặn thundering-herd → test concurrency + benchmark refresh trước deploy.
- U1 daily-baseline tốn 24-48h → chấp nhận, không để chặn unit chỉ cần số liệu intraday.
- Scale-out (U3) tăng tải MDS/Redis/worker → U2 phải chạy trước/song song để không "giải nghẽn runner, dồn nghẽn xuống worker".

## Rollout

Mỗi unit deploy + verify độc lập, không bundle. U1 trước tiên (an toàn, chỉ đo). U2 (đánh giá worker/DB) trước khi đổ công vào micro-opt runner. Có thể dừng ở bất kỳ unit nào nếu số liệu không cho thấy lợi ích rõ so với rủi ro.

---

Unresolved Questions:
- 100-150 alpha cần đạt trong bao lâu? (quyết định ưu tiên nhánh scale-readiness vs micro-opt)
- Benchmark U2/U3/U5 chạy trên server thật hay local simulate?
- Nếu U2 kết luận worker/SQLite là nút thắt: mở plan tối ưu tầng đó ngay, hay chấp nhận trần hiện tại tới khi thực sự cần 100-150?
- Giữ nguyên watchdog threshold (1d=120s...) hay điều chỉnh cùng đợt?
- Áp dụng cho `alpha-runner-legacy` cùng lúc hay chỉ `alpha-runner`?
- Mỗi runner instance cần resource limit riêng (CPU/mem cgroup) hay share host như hiện tại?
