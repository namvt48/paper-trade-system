# PLAN — Khôi phục feed 1d + recovery nhịp lỡ + vá gốc

STATUS: **REVIEWING** (chờ đại diện chốt Unresolved Questions cuối file)
Author: Claude (Architect). Ngày: 2026-07-20. Liên quan: HANDOFF cập nhật 15.

## Summary

Sự cố: MDS ngừng sản xuất nến `1d` từ restart 11:47 UTC 19/07 → 6 daily alpha không nhận event 1d → không rebalance 00:00 19/07 (một phần) và 00:00 20/07 (toàn bộ). Đại diện đã chốt trình tự **Backfill → Recovery → Fix**.

Chặn kỹ thuật đã phát hiện: recovery replay đọc nến native-tf (1d) từ parquet+Redis; hiện parquet 1d chỉ tới 17/07, Redis 1d tới nến close 19/07 (open 18/07). Nến 1d open 19/07 (cho nhịp 20/07) **không tồn tại**, và lịch sử 1d quá nông (~3 nến) so với warmup_bars daily (max=60). → **Phải backfill lịch sử 1d sâu trước khi build recovery.**

## Problem Frame

- 6 daily alpha: `1d-kertrend`(warmup 39), `1d-vwaprev`(20), `1d-chmom`(21), `1d-iamp`(25), `ensemble-1d`(?), `1d-trend60cmf`(60).
- Recovery `market.py` cần `tail = eligible[-(warmup_bars+16):]` nến 1d ≤ candle recovery. → cần ~**76 nến 1d** liên tục/symbol (max 60+16) tính tới open 19/07.
- Universe: mỗi daily alpha có whitelist riêng (5 con ~197 symbol, trend60cmf ~137). Backfill phải phủ HỢP của mọi whitelist.
- Recovery tool hardcode `EXCLUDED_ALPHA=1d-trend60cmf` (không sinh point + validation đòi digest bất biến) → muốn cứu trend60cmf phải sửa tooling.
- Nhịp 00:00 19/07: iamp/kertrend/vwaprev ĐÃ emit lúc 09:15 (giá cũ 9h) — recover thêm 00:00 19/07 = double-rebalance. chmom/ensemble/trend60cmf: 19/07 trắng hẳn.

## Requirements

1. Không mất/không hỏng dữ liệu paper-trade production (staging→audit→promote, backup, validation — như update 5/8).
2. Nến 1d backfill phải authoritative (Binance REST), đúng format recovery đọc.
3. Backfill KHÔNG phá MDS live (ghi Redis 1d không đè nhầm tf khác; không restart MDS ngoài kế hoạch).
4. Root-cause fix: 1d chạy bền vững qua restart giữa ngày; có observability chặn tái phát âm thầm.
5. Mọi code change có test; deploy MDS off-peak (tránh 23:50–00:40 UTC).

## Key Decisions

- **Nguồn backfill = Binance REST 1d klines** (không rollup từ 15m): authoritative, đơn giản, khớp cách MDS warmup fetch. Rollup từ parquet 15m rủi ro thủng/độ sâu.
- **Ghi backfill vào Redis `kline_snapshot_v2:binance:1d:<symbol>` (list, newest-first)** đủ cho recovery (merge parquet+Redis). Ghi parquet 1d = tùy chọn (để MDS live cũng có history) — quyết ở UQ.
- **Recovery gồm cả trend60cmf**: sửa `scope.INCIDENT_SCHEDULES` thêm trend60cmf (1d,1) + đổi validation `_excluded_alpha_check` thành có điều kiện (chỉ đòi bất biến khi KHÔNG nằm trong `only_alphas`). Có test.
- **Phạm vi nhịp recover**: chmom/ensemble/trend60cmf = cả 19+20/07; iamp/kertrend/vwaprev = **chỉ 20/07** (tránh double-rebalance vì đã có 09:15) — chờ đại diện xác nhận (UQ1).
- **Root-cause = thêm `1d` vào TIMEFRAMES + warmup luôn fetch 1d/12h**: đơn giản, trị gốc "restart giữa ngày thủng cascade". Không cố sửa logic restore phức tạp trừ khi cần.

## Implementation Units

### Phase 0 — Diagnostic (READ-ONLY) ✅ ĐÃ CHẠY
- Chạy `build --only-alpha` 5 daily, window 19-20/07 (run-id=diag-1d-20260720). KẾT QUẢ: **fail-loud `MarketCaptureError: insufficient 1d coverage for 1d-kertrend: 11/197; required=178`**. → Chỉ 11/197 symbol đủ ≥39 nến 1d. XÁC NHẬN backfill là prerequisite cứng. Không đụng production.

### Phase 1 — Backfill 1d
- Script mới `scripts/backfill_1d_klines.py` (uv PEP723): input = union whitelist 6 daily alpha; fetch Binance REST `/fapi/v1/klines?interval=1d&limit=~90` qua proxy-router; map sang KlineCandle dict (fields khớp snapshot: symbol,tf,open,high,low,close,volume,open_time,close_time,confirmed=true,exchange="binance"); ghi Redis list newest-first (dedup theo open_time, merge với hiện có, KHÔNG xóa nến khác). Idempotent.
- Acceptance: `kline_snapshot_v2:binance:1d:<sym>` có nến liên tục ≥76 ngày tới open 19/07 cho ≥95% symbol; nến open 19/07 close 20/07 tồn tại; BTCUSDT khớp giá Binance.
- Verify: rerun Phase 0 build → coverage 1d ≥ warmup_bars cho ≥90% symbol mỗi alpha.

### Phase 2 — Recovery (staged, mutate production)
- Code: `scope.py` (thêm trend60cmf + `only_alphas` gate cho excluded), `validation.py` (conditional excluded check), test cập nhật. Verify local `make test` recovery suite GREEN.
- Vận hành (như update 8): dừng worker+alpha-runner(+legacy) → `build --only-alpha ×6 --start 2026-07-19 --end 2026-07-20` → audit ledger/validation PASS → `promote --services-stopped` (backup + row-merge) → restart writers → reconcile ghost redis.
- Acceptance: 6 daily alpha có signal/position/trade/equity cho nhịp đã chọn; validation 10/10 PASS; alpha ngoài scope digest bất biến.

### Phase 3 — Root-cause fix (MDS, deploy off-peak)
- `market-data-service/.env`: `TIMEFRAMES=15m,1h,4h,12h,1d` (thêm lại 1d).
- Warmup/restore: đảm bảo sau restart giữa ngày, 1d/12h được dựng lại đủ (warmup fetch trực tiếp 1d + 12h cho mọi symbol, HOẶC restore_from_redis gồm 1d). Test tái hiện "restart 12:00 → 1d vẫn publish 00:00 kế".
- Observability: alert khi tf đã subscribe mà 0 event vượt cadence (1d>25h) — runner metrics + MDS `candles_processed_total{tf=1d}==0` guard.
- Deploy MDS off-peak, verify 1d publish ở daily close kế + `candles_processed_total{tf=1d}` tăng.

## Rollout / Safety

- Phase 1 ghi Redis MDS live — chạy off-peak, backup key trước (DUMP), chỉ đụng key `:1d:`.
- Phase 2 promote = điểm rủi ro cao nhất: services-stopped + backup + validation + rollback tự động (đã có trong tool).
- Phase 3 restart MDS = chính tác nhân gây sự cố → chỉ off-peak, có kế hoạch verify 1d ngay sau.

## Quyết định đại diện (2026-07-20)
- **Phase 1**: viết script → dry-run vài symbol → đại diện duyệt → ghi toàn bộ. (an toàn nhất)
- **UQ1 → chỉ recover 20/07** cho iamp/kertrend/vwaprev (giữ nhịp 09:15). Suy ra scope recovery:
  - chmom, ensemble-1d: recover **19/07 + 20/07** (trắng hẳn cả 2).
  - iamp, kertrend, vwaprev, trend60cmf: recover **20/07** only (trend60cmf 19/07 đã đúng; 3 con kia có 09:15).
  - → 2 build/promote riêng: (a) chmom+ensemble window 19-20; (b) 4 con còn lại window 20-20.
- **Phase 3 → deploy MDS off-peak NGAY** (trị gốc dứt điểm, không chờ tự lành).

## Unresolved Questions (còn lại)
- UQ2: Backfill 1d ghi luôn parquet không, hay Redis-only đủ? (mặc định Redis-only; parquet để MDS live cũng có history — sẽ quyết khi viết script)
- UQ4: ensemble-1d warmup_bars + phụ thuộc member (chmom) — xác nhận backfill đủ (verify qua rerun diagnostic build).
