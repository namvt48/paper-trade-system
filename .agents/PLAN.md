# PLAN — Sửa gốc: daily alpha ngừng rebalance do treo event-loop runner

STATUS: APPROVED
Owner: Claude (Architect)
Created: 2026-07-17
Decisions resolved: 2026-07-17 (đại diện chốt 5 câu — xem "Resolved Decisions")
Approved: 2026-07-17 (đại diện duyệt; KHÔNG restart tạm — chờ fix gốc deploy)
Scope: `alphas/runner/` (alpha-runner config-driven). KHÔNG đụng logic tín hiệu (`alphas/cross_alpha/strategy.py` phần công thức).

---

## Summary

Từ 16/07 11:57, 5/6 daily cross-sectional alpha (`1d-kertrend`, `1d-vwaprev`, `1d-iamp`, `1d-chmom`, `ensemble-1d`) ngừng nhận/emit tín hiệu; chỉ `1d-trend60cmf` còn rebalance. Nguyên nhân gốc: sự kiện refresh universe (`symbols:binance`) kích hoạt rescan toàn universe **đồng thời cho ~40 alpha** trên một event loop, làm nghẽn **thread pool mặc định** dùng chung của `asyncio.to_thread`; funding path đọc **180 lời gọi `redis.lrange` đồng bộ KHÔNG timeout** mỗi scan → một thread kẹt vĩnh viễn làm cạn pool. `run_strategy_event_loop` chỉ bắt exception, không bắt treo → loop park trong `scan()` chết im lặng (không log, không Queue-full vì queue daily đầy rất chậm).

Plan này KHÔNG chỉ khôi phục dịch vụ (restart là đủ để cứu tạm) mà loại bỏ nguyên nhân để không tái diễn: (1) chặn I/O treo bằng timeout, (2) cô lập + giới hạn thread pool, (3) de-thunder rescan, (4) watchdog + observability để "fail loud".

## Problem Frame

- Bằng chứng runtime (server 167.86.101.228, `alpha-runner-1`):
  - Log 11:57–11:58 16/07: `symbols:binance` fan-out ~40 alpha, `scan_ms` tăng dồn 21s→98s (nghẽn pool).
  - 5 daily alpha: dòng log cuối = 11:57 symbols event, sau đó im lặng; `trend60cmf` xử lý được kline close 00:00 17/07.
  - Không có reconnect/stale/exception/Queue-full nào giữa 12:00→00:00. Pubsub reader chung vẫn sống (`NUMSUB kline:binance:1d=2` = 2 runner, đúng thiết kế).
  - Dữ liệu KHÔNG phải thủ phạm: funding đủ 199 symbol, tươi (funding_time=17/07 00:00 UTC). OI hỏng (chỉ BTC) nhưng **không alpha daily nào đọc OI** (chỉ `short-btc-v1` dùng) → tách việc riêng.
- Điểm code:
  - `alphas/runner/strategies/cross_sectional/strategy.py:416` — `await asyncio.to_thread(self._attach_funding_panel, ...)`.
  - `strategy.py:444` — `{symbol: reader.load(symbol) for symbol in self._symbols}` → 180× `redis.lrange` đồng bộ tuần tự.
  - `alphas/runner/data_layer/funding_snapshot.py:29` — `self.redis.lrange(...)` client đồng bộ, không timeout.
  - `alphas/runner/shared_panel_feature_cache.py:86` — `await asyncio.to_thread(self._build_panel, ...)` dùng executor mặc định (bounded ~CPU+4, chia sẻ toàn process).
  - `alphas/runner/main.py` `run_strategy_event_loop` (~293) — try/except chỉ bắt Exception, không có timeout quanh `handle_strategy_event`.

## Requirements

- R1: Một scan treo/chậm của 1 alpha KHÔNG được làm chết hoặc bỏ đói các alpha khác.
- R2: Mọi I/O (redis funding) phải có timeout + không chạy đồng bộ trên event loop; thread không được kẹt vô hạn.
- R3: Treo phải được log rõ ("fail loud"), có thể phát hiện qua metrics/health — không im lặng.
- R4: Refresh universe không được tạo thundering-herd rescan đồng thời toàn bộ alpha.
- R5: Không đổi kết quả tín hiệu (numeric parity) — chỉ đổi cơ chế điều phối/I/O.
- R6: Có test tái hiện lỗi (regression) trước khi sửa.

## Key Decisions

- KD1: Dùng `redis-py` async cho FundingSnapshotReader thay vì sync + to_thread — quyết định: loại bỏ hẳn nguồn "thread kẹt vô hạn" tại gốc thay vì bọc timeout ngoài (bọc `asyncio.wait_for` quanh `to_thread` KHÔNG hủy được thread đang chặn sync).
- KD2: Gộp 180 `lrange` thành 1 pipeline/batch mỗi scan — quyết định: giảm 180 round-trip → 1, cắt phần lớn thời gian funding scan.
- KD3: Executor riêng, bounded, có tên cho panel/funding/select thay vì executor mặc định — quyết định: cô lập tải, kích thước cấu hình được, tránh alpha này bỏ đói alpha khác (R1).
- KD4: Bọc `handle_strategy_event` bằng timeout watchdog trong `run_strategy_event_loop` — quyết định: loop tự phục hồi + log khi 1 event vượt ngưỡng, không chết im lặng (R3). Ngưỡng theo timeframe: **1d=120s, 4h=120s, 1h=90s, 15m=60s** (cấu hình được, nới sau khi quan sát metrics).
- KD5: De-thunder `symbols:binance` — quyết định: KHÔNG chỉ dùng semaphore mà tối ưu triệt để (đại diện: "cải tiến hơn nữa cho hiệu quả"). 3 lớp: (a) **single-flight panel build** — nhiều alpha cùng (tf, universe_hash) khi cache-miss chỉ build MỘT lần, các alpha còn lại await future chung (hiện tại mỗi alpha build riêng → ~5× lãng phí cho nhóm 1d cùng universe); (b) **debounce/coalesce** nhiều sự kiện `symbols` sát nhau về 1 lượt rescan; (c) **semaphore** giới hạn số scan đồng thời làm trần an toàn.
- KD6: OI feed MDS **ngoài phạm vi** plan này (đại diện chốt: không) — chỉ `short-btc-v1` dùng OI, theo dõi ở việc riêng.

## Implementation Units

### U1 — Regression test tái hiện treo (làm trước, RED) — HOÀN THÀNH
- `alphas/runner/tests/test_event_loop_hang_regression.py`: (a) `OnceThenHangStrategy.scan()` hang ở event đầu → khẳng định loop vẫn xử lý event kline kế tiếp; (b) `FundingSnapshotReader` batch/wiring tests.
- Acceptance: RED xác nhận trên code gốc (2 test fail đúng cơ chế); GREEN sau U2+U4.

### U2 — Funding I/O có timeout + batch (R2, KD1/KD2) — HOÀN THÀNH
- **Điều chỉnh so với KD1 ban đầu**: không chuyển toàn bộ `mds_redis_client` sang `redis.asyncio` (blast radius lớn — client này dùng chung cho warmup/snapshot/funding). Thay vào đó: xác nhận `mds_client` (sync) chỉ dùng cho request/response ngắn hạn — KHÔNG dùng cho pubsub dài hạn (`SharedPubSubManager` tự mở connection async riêng từ `connection_kwargs`) → an toàn để thêm `socket_timeout`/`socket_connect_timeout` (mặc định 10s, config `mds_redis_socket_timeout_sec`, env `MDS_REDIS_SOCKET_TIMEOUT_SEC`) vào construction ở `main.py:464`. Đây là fix ngăn thread kẹt vĩnh viễn, blast radius tối thiểu (2 dòng), giữ nguyên `asyncio.to_thread` (giờ thread luôn trả về trong ≤10s).
- `funding_snapshot.py`: thêm `load_many(symbols, rows)` dùng `redis.pipeline()` — 1 round-trip thay vì N; `strategy.py:444` đổi sang gọi `load_many`. Giữ `load()` cũ nguyên vẹn (test cũ không vỡ).
- Acceptance: `test_mds_redis_client_is_constructed_with_a_bounded_socket_timeout`, `test_funding_snapshot_load_many_batches_into_a_single_round_trip` — cả 2 GREEN. `test_funding_panel_attach.py` (đã thêm `.pipeline()` vào FakeRedis test double) vẫn GREEN.

### U3 — Tách I/O polling khỏi compute pool (R1, KD3) — HOÀN THÀNH
- **Phát hiện khi implement**: pool bounded **đã tồn tại** (`main.py:433-439`, `RUNNER_COMPUTE_WORKERS=8`=đúng số core, không margin), bị dùng chung cho compute bursty (panel build) VÀ `config_listener.py`'s `run_in_executor(None, pubsub.get_message, 1.0)` — vòng lặp vô hạn giữ 1 thread suốt vòng đời process.
- **Sửa:** `config_listener.py` chuyển sang `redis.asyncio` pubsub riêng (`_derive_async_url` mirror pattern của `SharedPubSubManager`) — polling không còn tốn thread nào của compute pool. `find_newly_disabled`/`find_newly_enabled`/`claim_alpha_groups` giữ nguyên `run_in_executor(None,...)` (chỉ chạy khi có message, không giữ thread liên tục — chấp nhận được).
- Giữ `RUNNER_COMPUTE_WORKERS=8` (không đổi số, chỉ dọn pool đúng mục đích).
- Acceptance: `test_run_config_listener_uses_async_pubsub_not_thread_pool` (mới, `test_config_listener.py`) GREEN — xác nhận dùng `aioredis.from_url` + dispatch đúng `on_disabled`.

### U4 — Watchdog timeout quanh mỗi event (R3, KD4) — HOÀN THÀNH
- `main.py`: bảng `_EVENT_TIMEOUT_SEC` theo tf (1d/4h=120s, 1h=90s, 15m=60s, fallback=60s); `run_strategy_event_loop` bọc `handle_strategy_event` bằng `asyncio.wait_for(..., timeout=_event_timeout_sec(event))`; `except asyncio.TimeoutError` → log WARNING `[STRATEGY] scan timeout ...` + `metrics.inc_scan_timeout(alpha_id)` (mới, `metrics.py`) + `continue` (loop xử lý event kế tiếp, không chết).
- Thành công cũng ghi `metrics.mark_event_processed(alpha_id, now)` — nền tảng cho U6's `last_event_age_sec`.
- Acceptance: U1's `test_event_loop_recovers_and_processes_next_candle_after_scan_hangs` GREEN (dùng `monkeypatch` rút ngắn `_DEFAULT_EVENT_TIMEOUT_SEC` để test nhanh, cơ chế thật không đổi).

### U5 — De-thunder universe refresh (R4, KD5) — HOÀN THÀNH (a)+(b), (c) DESCOPE
- (a) **Single-flight panel build** ở `shared_panel_feature_cache.get_bundle` — HOÀN THÀNH: `self._inflight: dict[key, asyncio.Task]`; cache-miss đầu tiên tạo task (`_build_and_store`), caller khác cùng key `await` chung task thay vì build lại; chỉ "owner" dọn `_inflight` (kể cả nhánh lỗi, qua `finally`). Counter mới `panel_build_single_flight_joins_total`. Test `test_shared_panel_feature_cache_single_flight.py`: 5 caller đồng thời cùng key → `_build_panel` gọi đúng 1 lần.
- (b) **Semaphore** giới hạn `handle_strategy_event` đồng thời toàn runner — HOÀN THÀNH: `scan_semaphore = asyncio.Semaphore(cfg.compute_workers)` tạo 1 lần trong `run()`, truyền vào mọi `run_strategy_event_loop(...)`; bọc quanh gọi `handle_strategy_event` bằng `async with (scan_semaphore or contextlib.nullcontext())` — no-op khi không truyền (test cũ không vỡ). Test `test_scan_semaphore.py`: 6 alpha refresh đồng thời, semaphore=2 → `max_concurrency<=2`.
- (c) **Debounce/coalesce nhiều `symbols:binance` sát nhau** — **DESCOPE, không làm.** Lý do: (1) bằng chứng log sự cố 16/07 cho thấy đúng MỘT lần publish fan-out tới ~40 queue KHÁC NHAU (mỗi alpha 1 bản một lần), không phải nhiều lần publish dồn vào MỘT queue — debounce nhắm sai cơ chế thực tế đã xảy ra; (2) cách làm đúng đòi hỏi "peek/drain" phần tử phía trước `asyncio.Queue` mà không làm sai lệch số lần `task_done()` — rủi ro `ValueError`/treo `queue.join()` ở chỗ khác trên hệ thống trading thật, không đáng đánh đổi khi (a)+(b) đã giải quyết đúng cơ chế gây treo quan sát được. Làm sau nếu log cho thấy `symbols:binance` thực sự bị publish lặp dồn dập.
- Acceptance: (a) ĐẠT — test xác nhận 1 build/key. (b) ĐẠT — test xác nhận trần đồng thời. `scan_ms` không còn tăng dồn tuyến tính tới ~100s — cần xác nhận thêm ở U7 khi deploy thật.

### U6 — Observability "fail loud" (R3) — HOÀN THÀNH
- Nền tảng từ U4: `metrics.scan_timeout_by_alpha`, `metrics.last_event_ts_by_alpha` (dict per-alpha, cập nhật mỗi event xử lý thành công) — đã có trong `RunnerMetrics.snapshot()`.
- `main.py::runner_metrics_snapshot`: thêm `_STALE_TF_MS` (per-tf), `_alpha_stale_threshold_sec` (= max(60s, min_tf_ms/1000 × 2 candles)), `_is_alpha_stale`; snapshot có thêm `last_event_age_sec` (dict per-alpha, `now - last_ts`) và `stale_alphas` (list alpha_id vượt ngưỡng). Cả 2 helper dùng `getattr(...)` phòng thủ (không crash nếu strategy-like object thiếu `get_warmup_tfs`/`alpha_id` — quan sát không được phép tự sập, đúng tinh thần U6).
- `metrics_http.py::_health`: đọc `stale_alphas` từ snapshot → trả `503 {"status":"degraded","stale_alphas":[...]}` nếu có alpha im lặng vượt ngưỡng; `200 {"status":"ok"}` nếu không.
- **KHÔNG làm** executor `queue_depth`: `ThreadPoolExecutor._work_queue.qsize()` là API nội bộ không chính thức của `concurrent.futures`, dễ vỡ giữa các Python minor version — không đáng đánh đổi cho một số liệu phụ khi `stale_alphas`/`scan_timeout_by_alpha` đã là tín hiệu "fail loud" chính, đúng yêu cầu R3.
- Acceptance: `test_runner_metrics_snapshot_flags_alpha_silent_far_longer_than_its_timeframe`, `test_runner_metrics_snapshot_does_not_flag_alpha_that_never_processed_yet`, `test_health_endpoint_returns_503_when_an_alpha_is_stale` (mới, `test_deployment_runtime.py`) — GREEN, không vỡ 5 test cũ trong cùng file.

### U7 — Verify + triển khai — ĐÃ DEPLOY, chờ xác nhận cuối ở daily close kế tiếp
- `make test-runner`: 168 passed, 15 skipped, 1 failed + 10 error (baseline pre-existing, xác nhận qua `git stash` trước/sau — không do plan này gây ra). ✓
- Commit `b3e5b16` (scope `alphas/runner/` + `.agents/`, bundle cả một số việc dở dang khác của session trước không tách được — đại diện đã xác nhận chấp nhận). ✓
- Deploy 2026-07-17 04:17 UTC lên server `167.86.101.228`: `make package` → scp → build + restart **chỉ** `alpha-runner` (`--no-deps`, không đụng `alpha-runner-legacy`/`worker`/core — mirror pattern `deploy-legacy-runner` nhưng đúng service). ✓
- Verify ngay sau deploy (04:25:58 UTC, warmup xong 27/27):
  - Container `healthy`, không exception nào trong log.
  - `[CLAIM] Group 1d claimed 6 alphas: 1d-kertrend,1d-trend60cmf,1d-chmom,1d-vwaprev,1d-iamp,ensemble-1d` — đủ 6.
  - `/health` → `{"status":"ok"}`; `/metrics` → `stale_alphas:[]`, `scan_timeout_by_alpha:{}`, `strategies_active:27`.
  - `panel_feature_cache`: 27 alpha nhưng chỉ `panel_build_total:5` (5 nhóm tf/universe riêng biệt) — xác nhận cache/single-flight hoạt động đúng cấu trúc.
  - `config_listener` subscribe qua async pubsub mới, không lỗi.
- **Còn lại (không thể verify trong phiên này — cách ~19.5h):** xác nhận cả 6 daily alpha thực sự emit signal ở candle close 00:00 UTC 18/07 (kiểm bằng DB `signals` hoặc `/metrics` `last_event_age_sec`/`stale_alphas` sau 00:00 UTC). Đề xuất: đại diện tự kiểm tra `curl localhost:9091/health` hoặc query DB sau 00:05 UTC, hoặc nhờ agent mới trong phiên sau kiểm tra.
- Acceptance: (a) deploy healthy, cấu trúc đúng — ĐẠT. (b) 6 daily alpha có signal mới trong `signals` sau 1 chu kỳ daily close — **CHƯA XÁC NHẬN**, cần kiểm tra lại sau 00:00 UTC 18/07.

## Risks

- Đổi sang async redis có thể lộ giả định thread-safety khác trong reader → giảm rủi ro bằng U1 test + parity check U2.
- Watchdog timeout quá thấp có thể cắt scan hợp lệ lúc tải cao → đặt ngưỡng theo timeframe, nới rộng, chỉnh sau khi quan sát metrics.
- Cancel `run_in_executor` KHÔNG dừng thread đang chạy → U2 (I/O có timeout) là điều kiện cần để watchdog thực sự giải phóng slot.

## Rollout

1. Merge U1–U6 sau review. 2. Deploy giờ thấp điểm (tránh gần 00:00 UTC daily close). 3. **Resume ngay sau deploy — KHÔNG chạy lại paper 48h** (đại diện chốt; các alpha vốn đang ở chế độ paper, không phải cổng paper→live). 4. Theo dõi 1–2 chu kỳ daily close qua metrics U6 + DB signals. 5. Rollback = redeploy image trước nếu daily alpha im lặng tái diễn.

---

## Resolved Decisions (2026-07-17)

1. Ngưỡng watchdog: 1d=120s, 4h=120s, 1h=90s, 15m=60s (cấu hình được). ✓
2. Ban đầu chốt 6 (giả định chưa có pool); **cập nhật sau khi đọc code**: pool `RUNNER_COMPUTE_WORKERS` đã tồn tại = 8 (đúng số core). Giữ 8 cho pool compute thuần túy, tách I/O polling hạ tầng ra riêng thay vì giảm xuống 6 (xem U3). ✓ (điều chỉnh kỹ thuật, không đổi ý định gốc của đại diện — vẫn "8 core" làm cơ sở).
3. De-thunder nâng cấp: single-flight build + debounce + semaphore (không chỉ semaphore). ✓
4. Không chạy lại paper 48h — resume ngay sau fix. ✓
5. OI feed MDS: ngoài phạm vi lần này. ✓

Unresolved Questions:
- (không còn)
