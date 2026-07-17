# HANDOFF

## 2026-07-17 (cập nhật 2) — Claude (Architect)

### Việc đã làm
Triển khai xong U1–U6 của `.agents/PLAN.md` (APPROVED), theo TDD: viết test tái hiện lỗi trước (RED), sửa code, xác nhận GREEN, không phá vỡ test cũ (baseline có 1 fail + 10 error pre-existing không liên quan, xác nhận qua `git stash` trước khi bắt đầu).

- U1: `test_event_loop_hang_regression.py` — test tái hiện treo (RED trước khi sửa).
- U2: `mds_client` (main.py:464) thêm `socket_timeout`/`socket_connect_timeout` (config `mds_redis_socket_timeout_sec=10.0`, env `MDS_REDIS_SOCKET_TIMEOUT_SEC`); `FundingSnapshotReader.load_many()` gộp N `lrange` thành 1 pipeline; `_attach_funding_panel` dùng `load_many`.
- U3: `config_listener.py` chuyển sang `redis.asyncio` pubsub riêng — không còn chiếm thread trong compute pool dùng chung (`RUNNER_COMPUTE_WORKERS=8` giữ nguyên).
- U4: `run_strategy_event_loop` bọc `asyncio.wait_for` quanh `handle_strategy_event` theo ngưỡng tf (`_EVENT_TIMEOUT_SEC`: 1d/4h=120s, 1h=90s, 15m=60s); timeout → log WARNING + `metrics.inc_scan_timeout` + tiếp tục vòng lặp (không chết im lặng).
- U5: (a) single-flight panel build (`shared_panel_feature_cache.py::get_bundle` + `_build_and_store`, `_inflight` dict) — N alpha cùng (tf,universe) chỉ build 1 lần; (b) `scan_semaphore = asyncio.Semaphore(cfg.compute_workers)` chặn số `handle_strategy_event` đồng thời toàn runner; (c) debounce **descope có lý do** (xem PLAN.md U5).
- U6: `runner_metrics_snapshot` thêm `last_event_age_sec`/`stale_alphas` per-alpha; `metrics_http.py::_health` trả 503 khi có alpha im lặng vượt ngưỡng.

### File thay đổi
- `alphas/runner/main.py`, `config.py`, `config_listener.py`, `metrics.py`, `metrics_http.py`
- `alphas/runner/data_layer/funding_snapshot.py`
- `alphas/runner/shared_panel_feature_cache.py`
- `alphas/runner/strategies/cross_sectional/strategy.py`
- Test mới: `test_event_loop_hang_regression.py`, `test_shared_panel_feature_cache_single_flight.py`, `test_scan_semaphore.py`; test cập nhật: `test_config_listener.py`, `test_funding_panel_attach.py` (FakeRedis thêm `.pipeline()`), `test_deployment_runtime.py`.
- `.agents/PLAN.md` cập nhật đầy đủ tiến độ U1-U6 (mỗi unit ghi rõ HOÀN THÀNH + điều chỉnh so với dự kiến ban đầu nếu có).

### Verify
`make test-runner`: **168 passed, 15 skipped, 1 failed, 10 errors** — fail/error đều là baseline pre-existing (xác nhận qua `git stash` so sánh trước/sau, KHÔNG do thay đổi lần này gây ra): `test_snapshot.py::test_snapshot_rejects_stale_latest_candle`, `test_alpha_claim.py` (6 test), `test_periodic_claim.py` (4 test) — đều lỗi/error giống hệt trên code gốc chưa sửa. Không sửa vì ngoài phạm vi plan này.

### Trạng thái
- Code **chưa commit, chưa deploy**. Đang ở working tree local.
- U7 còn lại: deploy lên server `167.86.101.228` (`alpha-runner-1`) + xác nhận 6 daily alpha emit signal ở kline close 00:00 UTC kế tiếp qua DB `signals`.
- **Dừng lại trước khi deploy** vì đây là hành động ảnh hưởng hệ thống live (paper-trade thật đang chạy) — cần xác nhận người dùng trước khi: (a) commit, (b) push/deploy lên server, (c) restart `alpha-runner-1`.

### Bước tiếp theo
1. Người dùng xác nhận: commit các thay đổi?
2. Người dùng xác nhận: deploy lên server + restart `alpha-runner-1`? (giờ thấp điểm, tránh gần 00:00 UTC theo Rollout trong PLAN.md)
3. Sau deploy: theo dõi 1 chu kỳ daily close (00:00 UTC) — xác nhận cả 6 daily alpha có signal mới trong DB `signals`, không alpha nào `stale_alphas` trên `/health`.

---

## 2026-07-17 — Claude (Architect)

### Việc đã làm
Chẩn đoán tại sao chỉ `1d-trend60cmf` rebalance hôm nay, 5 daily alpha còn lại (`1d-kertrend`, `1d-vwaprev`, `1d-iamp`, `1d-chmom`, `ensemble-1d`) im lặng từ 16/07 11:57.

Kết luận nguyên nhân gốc: refresh universe (`symbols:binance`) gây rescan đồng thời ~40 alpha → nghẽn thread pool mặc định (`asyncio.to_thread`); funding path đọc 180× `redis.lrange` đồng bộ KHÔNG timeout → thread kẹt vĩnh viễn; `run_strategy_event_loop` không có watchdog → loop chết im lặng.

Dữ liệu KHÔNG phải thủ phạm: funding đủ 199 symbol + tươi (17/07 00:00 UTC). OI hỏng (chỉ BTC) nhưng chỉ `short-btc-v1` dùng → tách riêng.

### File thay đổi
- `.agents/PLAN.md` (mới) — plan sửa gốc, STATUS=DRAFT, 7 unit (U1 test RED trước → U2 async funding → U3 executor riêng → U4 watchdog → U5 de-thunder → U6 observability → U7 verify/deploy).

### Điểm code liên quan (chưa sửa)
- `alphas/runner/strategies/cross_sectional/strategy.py:416,444`
- `alphas/runner/data_layer/funding_snapshot.py:29`
- `alphas/runner/shared_panel_feature_cache.py:86`
- `alphas/runner/main.py` `run_strategy_event_loop` (~293)

### Trạng thái
- PLAN.md = **REVIEWING** — 5 Unresolved Questions đã được đại diện chốt (xem "Resolved Decisions" trong PLAN.md). Chờ APPROVED cuối cùng → chưa được implement.
- Quyết định đã chốt: watchdog 1d/4h=120s,1h=90s,15m=60s · RUNNER_COMPUTE_THREADS=6 (8core−2) · de-thunder = single-flight+debounce+semaphore · không paper 48h lại (resume ngay) · OI feed ngoài phạm vi.
- Dịch vụ hiện vẫn treo: 5 daily alpha chưa được cứu (chưa restart — ưu tiên plan trước).

### Bước tiếp theo
1. Đại diện duyệt APPROVED (PLAN.md REVIEWING → APPROVED).
2. Sau APPROVED: implement U1→U7 (test RED trước), verify `make test-runner`, deploy giờ thấp điểm, resume ngay.
3. (Tùy chọn, song song) restart `alpha-runner-1` để cứu tạm 5 daily alpha nếu muốn khôi phục ngay.
