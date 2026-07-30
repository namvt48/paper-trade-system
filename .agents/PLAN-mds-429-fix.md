# PLAN — MDS 429 fix (A1 + A2 + B1)

STATUS: APPROVED (đại diện chốt 2026-07-18: "cứ code đi, không cần quan sát quá nhiều" — nới tiêu chí quan sát 429 24-48h, dựa vào unit test là chính vì 429 chỉ bùng lúc reconnect)
TARGET REPO: `market-data-service/` (KHÔNG phải paper-trade-system)
OWNER: Codex/OpenCode implement · Claude review
RELATED: `.agents/HANDOFF.md` cập nhật 7-8 (root cause daily-alpha), điều tra 429

## Summary

Gộp 3 sửa nhỏ vào **1 PR trên `market-data-service`** để chặn burst 429: (A1) sửa lỗi khai báo thiếu weight ở reconnect gap-fill, (A2) single-flight + semaphore dùng chung cho reconnect gap-fill (chặn N batch reconnect cùng quét full-universe), (B1) giới hạn concurrency của funding poll. Tất cả behavior-preserving (vẫn gap-fill/poll đủ symbol), chỉ đổi pacing/dedup, có unit test, deploy an toàn ngoài cửa sổ 00:00 UTC. **C1 (điều tra vì sao WS reconnect 18 lần/ngày) tách task riêng, chạy SAU khi PR này deploy + quan sát.**

## Problem Frame

429 không do tải steady-state mà bùng theo cụm khi **WS kline reconnect → gap-fill hàng loạt qua REST**. Chuỗi khuếch đại (đã soi tới dòng code):
- `adapter.py:534`: reconnect gap-fill `acquire(weight=5)` nhưng fetch `limit=1500` → weight thật = `kline_weight(1500)=10`. Rate limiter **đếm thiếu 2×** → cho phép gấp đôi tốc độ → vượt trần Binance.
- `adapter.py:502-527`: mỗi WS batch reconnect (`kline_feed.py:106`) gọi `_on_ws_reconnect`, mà hàm này quét **TOÀN BỘ** `_last_candle_time` (≈520 symbol) chứ không chỉ batch đó. 00:31 có 3 batch reconnect trong 30s → **3× quét full-universe dư thừa**. Mỗi lần tạo `Semaphore(10)` MỚI → 30 REST đồng thời, không cap chung.
- `funding_feed.py:96`: `asyncio.gather(*all_symbols)` bắn 520 coroutine cùng lúc mỗi 300s → thundering herd; khi trúng cửa 429-pause → collateral-fail (199 fail dồn 1 cụm).

Hệ quả: candle một số symbol về trễ qua gap-fill (không event) → góp phần vào lỗi coverage-gate của daily alpha (HANDOFF cập nhật 7).

## Requirements

- R1. Reconnect gap-fill khai báo weight ĐÚNG bằng weight thật của request.
- R2. Nhiều reconnect gần/đồng thời KHÔNG tạo nhiều lượt quét full-universe — coalesce về 1.
- R3. Cap concurrency toàn cục cho reconnect gap-fill (không tạo semaphore mới mỗi call).
- R4. Funding poll giới hạn số REST in-flight, vẫn poll đủ mọi symbol trong 1 interval.
- R5. Không đổi hành vi dữ liệu: mọi symbol vẫn được gap-fill/poll; chỉ đổi tốc độ/dedup.
- R6. Có unit test cho từng thay đổi; toàn bộ suite MDS xanh; ruff/type sạch.
- R7. Deploy không rơi vào cửa sổ 00:00 UTC (daily close) và rollback được.

## Key Decisions

- **A1 dùng hằng số chung**: rationale — tránh drift giữa `acquire(weight=...)` và `limit=...`. Đặt `_RECONNECT_GAP_LIMIT = 1500`, dùng cho cả `acquire(weight=kline_weight(_RECONNECT_GAP_LIMIT))` lẫn `futures_klines(limit=_RECONNECT_GAP_LIMIT)`.
- **A2 dùng `asyncio.Lock` non-blocking + debounce timestamp** thay vì queue: rationale — các batch reconnect quét CÙNG một `_last_candle_time`, nên chỉ cần 1 lượt chạy; lượt đang chạy giữ lock, lượt đến sau thấy `locked()` hoặc còn trong debounce-window thì bỏ qua (coalesce). Không mất gap vì gap-fill quét snapshot hiện tại + per-symbol check `gap_ms>60s` vẫn còn.
- **A2 semaphore ở cấp instance**: rationale — cap concurrency thật sự toàn cục, không phải mỗi call một cái.
- **B1 dùng bounded-concurrency semaphore (không chunk cứng)**: rationale — rate limiter đã pace theo weight; chỉ cần chặn thundering-herd để không có 520 call in-flight khi 429 ập tới. Semaphore mượt hơn chunk.
- **Config hoá ngưỡng** (không hardcode): theo trading-system-rules. Thêm `BINANCE_FUNDING_POLL_CONCURRENCY`, `BINANCE_RECONNECT_GAP_CONCURRENCY`, `BINANCE_RECONNECT_GAP_DEBOUNCE_SEC`.
- **KHÔNG nâng `BINANCE_RATE_LIMIT_WEIGHT_PER_MINUTE` trong PR này**: rationale — nới trần trước khi sửa weight-count là phản tác dụng. Để riêng, sau khi A1 xác nhận giảm 429.

## Implementation Units

### U1 — A1: sửa weight under-declaration
- File: `app/adapters/binance/adapter.py` (`_on_ws_reconnect._fill_reconnect_gap`, ~L527-541).
- Thêm hằng `_RECONNECT_GAP_LIMIT = 1500` (cấp module). `acquire(weight=5)` → `acquire(weight=kline_weight(_RECONNECT_GAP_LIMIT))`; `limit=1500` → `limit=_RECONNECT_GAP_LIMIT`. (`kline_weight` đã import ở L13.)
- Test (`tests/test_binance_adapter.py`): stub client + rate_limiter spy → assert `acquire` được gọi với `weight == kline_weight(1500)` (=10) cho mỗi symbol gap-fill.

### U2 — A2: single-flight + shared semaphore reconnect gap-fill
- File: `app/adapters/binance/adapter.py` (`__init__` + `_on_ws_reconnect`).
- `__init__`: `self._reconnect_gap_lock = asyncio.Lock()`, `self._reconnect_gap_sem: asyncio.Semaphore | None = None` (lazy-init trong event loop), `self._last_reconnect_gap_ts = 0.0`.
- Đầu `_on_ws_reconnect`: nếu `self._reconnect_gap_lock.locked()` HOẶC `monotonic() - self._last_reconnect_gap_ts < debounce_sec` → log "coalesced" + return. Ngược lại `async with self._reconnect_gap_lock:` bọc toàn bộ, set `_last_reconnect_gap_ts` sau khi xong.
- Dùng `self._reconnect_gap_sem` (khởi tạo 1 lần, size = `BINANCE_RECONNECT_GAP_CONCURRENCY`) thay cho `Semaphore(10)` cục bộ.
- Test: gọi `_on_ws_reconnect()` 3 lần đồng thời (asyncio.gather) → `futures_klines` chỉ được gọi đúng 1 lượt/symbol (không 3×); lần thứ 2/3 log coalesced.

### U3 — B1: bound funding poll concurrency
- File: `app/adapters/binance/funding_feed.py` (`run`), `__init__` thêm `poll_concurrency`.
- Thay `asyncio.gather(*(self._poll_once(s) ...))` bằng semaphore-bounded gather: `sem = asyncio.Semaphore(self._poll_concurrency)`, wrap `_poll_once` trong `async with sem`.
- Test (`tests/test_funding_feed.py`): stub `_poll_once` đếm concurrency đỉnh → assert ≤ `poll_concurrency`; assert cả `len(symbols)` symbol đều được gọi đúng 1 lần.

### U4 — Config
- File: `app/config.py`. Thêm: `BINANCE_FUNDING_POLL_CONCURRENCY: int = 15`, `BINANCE_RECONNECT_GAP_CONCURRENCY: int = 10`, `BINANCE_RECONNECT_GAP_DEBOUNCE_SEC: int = 30`. Wire vào chỗ khởi tạo funding feed + adapter.
- Test: mặc định đọc đúng; override qua env.

## Acceptance Criteria

- [ ] `make test-unit` + `make test-adapters` xanh (bao gồm U1-U4 test mới).
- [ ] Ruff + type check sạch.
- [ ] Đọc diff xác nhận R5: mọi symbol vẫn gap-fill/poll; không bỏ symbol.
- [ ] Sau deploy 24-48h (observability): số 429 mỗi cụm reconnect giảm rõ; funding-fail-burst giảm; readiness coverage daily alpha vẫn ~99% (không hồi quy độ tươi candle).

## Rollout / Deploy safety

1. Deploy: `make package` → `make deploy` (SERVER=root@167.86.101.228), **tránh cửa 23:50-00:40 UTC** (daily close). Chọn giờ ít hoạt động.
2. MDS restart tự gây 1 burst startup-gap (một lần, có `Semaphore(10)` + limiter sẵn) — chấp nhận được.
3. Rollback: redeploy build trước đó (`make deploy` bản cũ). Không đụng schema/DB nên rollback sạch.
4. Theo dõi: `docker logs market-data-service... | grep -c 429` theo cụm reconnect kế; `[WS-RECONNECT] Filling gaps` phải thấy log "coalesced" khi nhiều batch reconnect.

## Risks

- A2 debounce quá gắt → bỏ sót gap sau outage dài. Mitigate: debounce chỉ gộp reconnect ĐỒNG THỜI (window 30s); sau đó reconnect mới vẫn quét `_last_candle_time` hiện tại + per-symbol `gap_ms>60s` vẫn chạy → gap còn lại vẫn được vá.
- B1 concurrency thấp → funding poll lâu hơn. Kiểm: 520 symbol / 15 concurrent vẫn << interval 300s. An toàn.
- Đây là service dùng chung (feed mọi alpha) → bắt buộc test kỹ + deploy off-peak.

## Scope Boundaries (KHÔNG làm trong PR này)

- Không nâng rate limit weight (để riêng, sau A1).
- Không đổi `BINANCE_TOP_SYMBOLS`.
- Không chuyển funding sang WebSocket (E1, dài hạn).
- Không sửa runner/whitelist (việc daily-alpha coverage tách riêng).

## C1 — Tách task riêng (chạy SAU khi PR này deploy + quan sát)

Điều tra **vì sao WS kline reconnect 18 lần/ngày** (gốc của gốc). Giả thuyết: `ws_batch_size=100` quá lớn (Binance ngắt combined-stream lớn) / thiếu ping-keepalive / Binance 24h disconnect / mạng server. Deliverable: root cause + đề xuất mitigation (giảm batch size? thêm keepalive? backoff?). Ổn WS → gần như hết mass gap-fill → 429 biến mất tận gốc.

## Unresolved Questions

- Có 1 `_RateLimiter` DÙNG CHUNG cho mọi REST (kline/funding/oi/depth) hay nhiều instance? (ảnh hưởng việc nới trần sau này — cần xác nhận khi implement)
- `BINANCE_FUNDING_POLL_CONCURRENCY=15` OK hay muốn con số khác?
- Deploy MDS ai chạy (Codex/đại diện) và khung giờ off-peak nào?
