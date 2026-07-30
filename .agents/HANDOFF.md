# HANDOFF

## 2026-07-21 (cập nhật 20) — Claude: ROOT CAUSE cascade gap ĐÃ ĐIỀU TRA XONG + feed rebalance 21/07 vào DB (5 daily alpha)

### ROOT CAUSE THẬT của cascade gap (lần đầu điều tra tới gốc — trả lời câu hỏi treo ở cập nhật 15/19)
6 daily alpha KHÔNG rebalance 00:00 UTC 21/07. Kiểm chứng 3 tầng trên production:
- **Data**: 4h thiếu `1784520000000` (20/07 04:00→08:00); 12h thiếu `1784505600000` (00:00→12:00); 1d không có nến 20/07. NHƯNG 15m@04:00–07:45 + 1h@04:00–07:00 ĐỀU CÓ, liền mạch → lỗi đúng ở bước rollup, không phải thiếu nến nền.
- **Signal DB**: từ 00:00 UTC 21/07 nhiều alpha 15m/1h có signal, 0 alpha 1d nào.
- **Thời điểm**: MDS StartedAt=2026-07-20T04:07:37Z — restart 7' SAU khi bucket 4h 04:00–08:00 đã mở (deploy revert Change B ở cập nhật 18).

**Cơ chế (code market-data-service):** cascade tf-cao là rollup **trigger-driven, một-lần, có cổng cứng `if len(parts)<needed: return None`, KHÔNG retry**. Khi restart giữa bucket:
1. Mất 1m@04:00–04:06 → live không dựng nổi 15m@04:00/1h@04:00.
2. `adapter._fill_startup_gap` (dòng 493) fetch mỗi tf ĐỘC LẬP từ Binance, inject thẳng bằng `_append_or_replace` (BỎ QUA rollup), lại chỉ lấy nến đã ĐÓNG (bucket 04:00–08:00 chưa đóng lúc 04:11 → không lấp).
3. `aggregator.apply_correction` với nến tf≠1m chỉ append, `return None` (không re-trigger rollup).
4. 08:00: 1h@07:00 roll up → kích rollup 4h → 1h@04:00 chưa có → len<4 → bỏ qua VĨNH VIỄN. 1h@04:00 về sau (warmup) qua đường inject-trực-tiếp không re-trigger.
→ 4h mất → 12h mất → 1d@20/07 mất → 0 event 1d lúc 00:00 21/07 → 6 daily alpha đứng hình. Intraday (15m/1h) không ảnh hưởng.
**Bất nhất then chốt cần fix:** `_fill_startup_gap` dùng `_append_or_replace` (bỏ rollup) còn `_run_reconnect_gap_fill` (dòng 616) lại đi qua `on_1m_close` (có rollup). Tái diễn MỖI lần restart/deploy MDS không rơi đúng biên 4h (19/07 thiếu 4h@08:00 cùng cơ chế). KHÁC hẳn event-loop hang (b3e5b16, lỗi hạ nguồn runner).

### ĐÃ FEED REBALANCE 00:00 UTC 21/07 VÀO DB (đại diện chốt "chạy toàn bộ ngay")
5 daily alpha (`1d-kertrend/vwaprev/iamp/chmom/ensemble-1d`). `1d-trend60cmf` bị EXCLUDED_ALPHA loại cứng → CHƯA recover (vẫn silent từ 19/07).
1. Backfill 1d@20/07 vào mds-redis (6381): 197/197 ghi OK, backup `recovery/backfill-1d-redis-backup-20260721.json`.
2. Stop worker+alpha-runner+alpha-runner-legacy.
3. Build `rec-20260721` (--start/--end 2026-07-21, --only-alpha ×5): **validation=PASS**, 5 cycles/1596 signals, `approval_hash=f16961b6...`.
4. Promote --services-stopped → **COMPLETE** (redis paper 6382).
5. Restart 3 service → `make runner-reconcile` = 0 ghost key.
6. **Verify DB**: kertrend 294/vwaprev 332/iamp 306/chmom 314/ensemble 350 signal 21/07 (khớp cycle). trend60cmf=0 (đúng).

### CÒN LẠI / THEO DÕI
- **00:00 UTC 22/07**: cả 5 (và trend60cmf) tự lành NẾU MDS không restart giữa bucket (cascade 21/07 đang dựng sạch: 4h/12h tới 21/07 00:00 đủ). Nếu restart giữa bucket → tái diễn.
- **trend60cmf**: vẫn silent từ 19/07, tool không recover được. Cần quyết định: thêm vào INCIDENT_SCHEDULES + nới `_excluded_alpha_check`, hay để tự lành.
- **FIX GỐC MDS (chưa làm)**: thêm reconciliation-rollup sau mọi đường inject + thống nhất `_fill_startup_gap` đi qua rollup. Đây là fix đúng để hết tái diễn.
- Runner warmup ~2' sau restart (đang health:starting lúc bàn giao — bình thường).

---

## 2026-07-20 (cập nhật 19) — Claude: Seed parquet 1d + Recovery Phase 2 XONG trên PRODUCTION. Cả 6 daily alpha trắng lúc 00:00 UTC 20/07 do gap 4h@08-12/19/07 (mới, không chỉ 07-19 cũ)

### PHÁT HIỆN QUAN TRỌNG: session trước làm nhầm môi trường LOCAL thay vì PRODUCTION
Đại diện đưa runbook với path `/root/market-data-service` (production server 167.86.101.228). Ban đầu tôi hiểu nhầm và chạy toàn bộ Runbook 1 trên máy local (`~/.desktop/quant-space/...`) — vô nghĩa vì không phải hệ thống thật đang chạy paper-trade. Phát hiện ra khi build recovery ở local báo `EquityBuildError` cho symbol 0GUSDT — điều tra sâu mới lộ ra: (1) `PriceArchive` trong `equity.py` dùng nến **15m** (không phải 1d) để mark-to-market, (2) toàn bộ 529 symbol trên **local** đứng yên từ 2026-06-24 — vì đó chỉ là bản sao dev cũ, không phải lỗi thật. Đã xác nhận qua SSH: `root@167.86.101.228` có sẵn config, `/root/market-data-service` tồn tại thật. **Từ giờ mọi thao tác production PHẢI qua SSH vào 167.86.101.228, không dùng local path.**

### XÁC NHẬN LẠI TRÊN PRODUCTION (05:02 UTC 20/07)
Query trực tiếp `signals` table qua `docker compose exec worker`: **CẢ 6 daily alpha (kể cả `1d-trend60cmf` vốn luôn khỏe) đều 0 signal lúc 00:00 UTC 20/07 hôm nay.** Đây là lần đầu trend60cmf cũng bị ảnh hưởng — khác các lần trước chỉ 5 alpha kia bị.

Root cause xác nhận qua mds-redis (`docker exec redis-mds-redis-1 redis-cli LRANGE kline_snapshot_v2:binance:4h:BTCUSDT 0 8`): **thiếu 4h@[08:00–12:00 19/07]** trong cascade (00:00,04:00 có → nhảy thẳng 12:00,16:00,20:00) → 12h@[00:00-12:00 19/07] không hình thành → 1d@19/07 không hình thành → 0 event nào kích daily alpha lúc 00:00 20/07. Tin tốt: cascade 4h HÔM NAY (07-20) đã hình thành đúng ngay từ 00:00 (không lỗ thêm) → tiên đoán 1d@20/07 sẽ tự hình thành đúng 00:00 UTC 21/07, đưa mọi alpha (bao gồm trend60cmf) rebalance đúng giờ trở lại — miễn MDS không bị restart/crash giữa chừng.

### Runbook 1 (seed parquet 1d) — ĐÃ CHẠY TRÊN PRODUCTION, THÀNH CÔNG
`docker compose exec market-data-service python scripts/seed_parquet_cache.py --seed-tfs 1d --days 95 --no-materialize` → 530/530 symbol, 48,315 candle, 0 fail, ~5 phút. Log tự phát hiện + vá đúng lỗ `repair:2026-07-18→2026-07-20` cho nhiều symbol. Verify BTCUSDT: 94 nến liên tục 2026-04-16→2026-07-19, không lỗ hổng.

### Runbook 2 (Phase 2 Recovery) — ĐÃ PROMOTE LÊN PRODUCTION
Scope xác nhận qua lịch sử signal thật (không đoán): `1d-chmom`/`ensemble-1d` lỡ **07-19 VÀ 07-20** (2 nhịp, tín hiệu cuối 07-18T02:57); `1d-iamp`/`1d-kertrend`/`1d-vwaprev` đã có tín hiệu 07-19T09:15 (sai giờ, tool từ chối double-write) nên chỉ cần recover **07-20** (1 nhịp). `1d-trend60cmf` bị exclude cứng khỏi tool (`EXCLUDED_ALPHA` trong `scope.py`) — để tự lành tự nhiên, KHÔNG đưa vào recovery.

1. Dừng `worker`+`alpha-runner`+`alpha-runner-legacy` trên production.
2. Build `rec-a-20260720` (chmom+ensemble, --start 2026-07-19 --end 2026-07-20): **validation=PASS**, 4 cycles/1298 signals, `approval_hash=55bf2b7e...`.
3. Build `rec-b-20260720` (iamp+kertrend+vwaprev, --start/--end 2026-07-20): **validation=PASS**, 3 cycles/862 signals, `approval_hash=c38acfe8...`.
4. Promote rec-a → COMPLETE. Promote rec-b (hash cũ) → **FAIL "production main DB drifted after staging"** (đúng thiết kế guard — rec-a đã đổi production DB nên guard của rec-b build trước đó lệch). Build lại rec-b từ baseline mới → validation=PASS lại (giống hệt số liệu) → `approval_hash=98fac658...` → Promote → **COMPLETE**.
5. Restart worker+alpha-runner+alpha-runner-legacy → `make runner-reconcile` (0 ghost key) → alpha-runner warmup ~2 phút (197 symbol × 8640 bar 15m) rồi healthy.
6. **Verify**: cả 5 alpha có signal mới (chmom/ensemble total=1090/1194, open_positions=154/174; iamp/kertrend/vwaprev total=1446/1586/1532, open_positions=142/142/172). Health endpoint `{'status':'ok'}`. Log ALPHA_TIMING cho thấy scan live bình thường (`scanned=True ready=True`). 0 error liên quan tới 5 alpha này trong log worker (error khác thấy trong log là của alpha không liên quan — pre-existing, khớp P0 reconciliation cập nhật 6).

### CÒN LẠI / THEO DÕI
- **00:00 UTC 21/07 (đêm nay)**: xác nhận cả 6 daily alpha (bao gồm trend60cmf) tự rebalance đúng giờ — bằng chứng cascade hôm nay không lỗ. Nếu KHÔNG tự lành → 4h/12h cascade có vấn đề mới, cần điều tra lại.
- **Root cause cascade gap thật sự** (tại sao 4h@08-12/19/07 bị thiếu) vẫn CHƯA điều tra — đây là lần thứ 2+ xảy ra hiện tượng lỗ 4h/12h giữa ngày. Nếu tái diễn thường xuyên, cần xem lại cơ chế restore/warmup của MDS sau mỗi restart (liên quan Change A/B/C/D ở cập nhật 17-18).
- Commit code (recover_rebalances --only-alpha, MDS 6 hunk A/C/D): vẫn CHƯA commit từ các cập nhật trước — không đổi gì thêm session này (chỉ chạy seed script có sẵn + recovery tool có sẵn).

---

## 2026-07-20 (cập nhật 18) — Claude: Deploy Phase 3 gây OUTAGE ~10' (Change B hang) → ĐÃ REVERT + khôi phục. Root fix đổi hướng: seed PARQUET 1d

### SỰ CỐ (báo trung thực)
Deploy Phase 3 (04:00 UTC): **Change B (luôn overlay `restore_from_redis` sau parquet) làm HANG startup MDS** → outage feed ~10 phút. Nguyên nhân: `restore_from_redis` chạy `scan_iter` 10 lần trên TOÀN keyspace mds-redis (5 tf × 2 prefix) — ở production scale quá chậm/kẹt. Trước đây Change B chưa từng lộ vì restore_from_redis CHỈ chạy khi parquet rỗng (production parquet luôn có 156k candle → nhánh này chưa bao giờ chạy).
- **Phát hiện**: MDS unhealthy, log dừng sau "Restored 156027 from parquet", 5+ phút không tiến.
- **Xử lý**: revert Change B về conditional (chỉ restore_from_redis khi parquet rỗng) trong `app/main.py` + comment cảnh báo → scp + `docker compose up -d --build --force-recreate` → **MDS healthy trở lại** (adapter started, kline connected, freshness 0.2s).

### TRẠNG THÁI HIỆN TẠI (healthy)
- MDS `status:ok`, TIMEFRAMES active=`15m,1h,4h,12h,1d`, **`candles_processed{tf=1d}=507`** (1d chạy lại nhờ .env + restore republish). Backfill Redis 1d còn nguyên (LLEN=96).
- **Feed sẽ tự lành hoàn toàn 00:00 UTC 21/07** (live cascade dựng 1d từ data ngày 20). 6 daily alpha nhận event 00:00 21/07.
- Change A (dormant), C (.env 1d, active), D (feed-stale monitor, active) GIỮ. Change B (redis-overlay) BỎ.

### ROOT FIX ĐỔI HƯỚNG (theo gợi ý đại diện)
Redis-overlay sai vì quét keyspace. Đúng hơn = seed nến 1d liên tục thẳng vào **PARQUET** (đường restore CHÍNH, nhanh) bằng tool có sẵn `market-data-service/scripts/seed_parquet_cache.py` (fetch Binance per-tf gồm 1d, ghi base/delta parquet có audit/repair; `--seed-tfs 1d --days 95 --no-materialize --output-dir historical_cache`). Lưu ý: seed all ~665 symbol (không có filter symbol), ghi cùng dir CacheWriter live đang ghi → chạy off-peak, MDS chỉ đọc parquet lúc restart nên rủi ro thấp. CHƯA chạy — chờ xác nhận.

### CÒN LẠI
- Seed parquet 1d (root fix, thay Change B) — chờ chốt.
- **Phase 2 recovery** 5 alpha (chmom+ensemble 19+20, iamp/kertrend/vwaprev 20) — coverage đã đủ nhờ Redis backfill. Services-stopped + build + promote.
- Commit: `backfill_1d_klines.py` (paper-trade) + MDS hunks A/C/D (đã deploy A/C/D + revert B; đồng bộ local==server cho main.py). CHƯA commit.

---

## 2026-07-20 (cập nhật 17) — Claude: PHASE 3 fix gốc MDS implement+review XONG, ĐANG DEPLOY. Còn Phase 2 recovery

Tinh chỉnh root cause: `.env TIMEFRAMES` thiếu 1d chỉ là phụ (publish/rollup 1d live KHÔNG phụ thuộc nó). Gốc tái phát THẬT = `restore_from_cache` (parquet) dựng buffer có hố sau restart giữa ngày + `main.py` chỉ fallback Redis khi parquet rỗng → hố parquet không được lấp. Feed tự lành 00:00 21/07 bất kể; restart-now KHÔNG cho 1d sớm hơn.

### Quyết định đại diện (bổ sung)
- trend60cmf: **để tự lành 21/07** (không sửa EXCLUDED_ALPHA). Recovery Phase 2 = 5 con.
- Phase 3: **fix gốc đầy đủ**, deploy trước Phase 2.

### Phase 3 — implement (TDD, subagent) + review (Claude) XONG
market-data-service, 6 hunk (blast-radius verify local==server mọi file khác, chỉ 4 file code + 1 dòng .env DIFF = đúng Phase 3; BINANCE_TOP_SYMBOLS=0 khớp 2 bên):
- **A** `app/aggregator.py restore_from_redis`: sau extend → sort+`_dedupe_store` → union sạch, Redis thắng khi trùng open_time (an toàn khi gọi chồng parquet).
- **B** `app/main.py`: LUÔN overlay `restore_from_redis` sau parquet (bỏ else-only) → lấp hố parquet (vd 12h thiếu làm đói 12h→1d).
- **C** `.env`: TIMEFRAMES=15m,1h,4h,12h,**1d**.
- **D** observability: `app/metrics.py` gauge `last_candle_publish_unixtime{exchange,tf}` + `app/publisher.py` set trong publish_klines + `app/main.py` `_feed_stale_monitor` (5min, alert ERROR khi tf vượt cadence×1.5, grace 2×cadence) — fail-loud chống ẩn.
- Test: 31 pass liên quan; full 385 pass, 11 fail+3 collection error = pre-existing env-only (no local redis 6381 + thiếu pip `binance`), 0 regression. CHƯA commit.
- **ĐANG `make deploy` MDS** (03:50 UTC off-peak). Verify sau deploy: healthy + log "Restored N from Redis" (dù parquet có data) + snapshot 1d 94 nến còn + intraday resume + monitor chạy. **CHỜ 00:05 21/07 xác nhận 1d publish.**

### CÒN LẠI
- **Phase 2 recovery** (mutate production, services-stopped): 5 con — (A) chmom+ensemble start=2026-07-19 end=2026-07-20; (B) iamp+kertrend+vwaprev start=2026-07-20 end=2026-07-20. build → audit validation → promote --services-stopped → restart → reconcile. (Coverage đã đủ nhờ backfill; đã verify build qua coverage gate.)
- Commit: `backfill_1d_klines.py` (paper-trade) + 6 hunk MDS (market-data-service) — CHƯA commit cả 2 repo.

---

## 2026-07-20 (cập nhật 16) — Claude: PHASE 1 BACKFILL 1d XONG (proven). Còn Phase 2 recovery + Phase 3 fix gốc

Theo PLAN `.agents/PLAN-1d-feed-recovery.md` (Backfill→Recovery→Fix, đại diện chốt). Trình tự chốt: iamp/kertrend/vwaprev recover CHỈ 20/07 (giữ nhịp 09:15); chmom+ensemble recover 19+20; trend60cmf 20 only; Phase 3 deploy MDS off-peak ngay.

### ĐÃ LÀM
- **Diagnostic build (read-only)** window 19-20/07: fail `insufficient 1d coverage 1d-kertrend 11/197 (cần 178)` → chứng minh recovery bị chặn do thiếu lịch sử 1d.
- **Phase 1 backfill** `scripts/backfill_1d_klines.py` (MỚI, PEP723, default dry-run, `--apply` mới ghi, backup rollback): fetch 94 nến 1d/symbol từ Binance fapi (host reach trực tiếp 200) → merge vào Redis `kline_snapshot_v2:binance:1d:<sym>` (Binance thắng khi trùng, newest-first, ltrim 300). **197/197 ghi OK, 0 fail**. Backup: `recovery/backfill-1d-redis-backup.json` trên server. Giá verify khớp Binance + khớp 12h/4h live (BTC open19/07 O=64806.7 C=64694.7).
- **Verify Phase 1**: rerun build → coverage gate QUA (hết MarketCaptureError). Fail mới = `ReplayError: target cycles already contain signals: iamp/kertrend/vwaprev@1784332800000` (nến open 18/07 = nhịp 19/07, chính là signal 09:15 đã có) → xác nhận đúng scope split.

### CÒN LẠI
- **Phase 2 recovery** (mutate production, services-stopped): 
  - Cần code change: thêm `1d-trend60cmf` vào `scope.INCIDENT_SCHEDULES` + `validation._excluded_alpha_check` thành conditional (chỉ đòi bất biến khi KHÔNG trong only_alphas) + test. **HOẶC** bỏ trend60cmf khỏi recovery, để tự lành 00:00 21/07 (tránh đụng safety check) — CHỜ đại diện.
  - 2 build/promote: (A) chmom+ensemble start=2026-07-19 end=2026-07-20; (B) iamp+kertrend+vwaprev(+trend60cmf nếu chốt) start=2026-07-20 end=2026-07-20. Stop worker+alpha-runner(+legacy) → build → audit validation → promote --services-stopped → restart → reconcile ghost redis.
- **Phase 3 fix gốc MDS** (deploy off-peak): `.env TIMEFRAMES` thêm `1d`; vá warmup/restore để restart giữa ngày không thủng cascade 1d; observability alert tf-subscribed-0-event. Deploy tránh 23:50–00:40 UTC.
- backfill_1d_klines.py CHƯA commit (local + đã scp server).

---

## 2026-07-20 (cập nhật 15) — Claude: 6 daily alpha KHÔNG rebalance 00:00 20/07 = MDS NGỪNG SẢN XUẤT NẾN 1d (mode lỗi MỚI, thượng nguồn)

Câu hỏi đại diện: tại sao vẫn nhiều alpha chưa rebalance. Điều tra live server 03:01 UTC 20/07.

### KẾT LUẬN (đã kiểm chứng mọi tầng: DB → runner metrics → MDS Redis → MDS metrics)
**MDS ngừng publish nến `1d` kể từ restart ~11:47 UTC 19/07 (deploy update-13). Không event `kline:binance:1d` nào tới runner → 6 daily alpha không có nhịp scan → không rebalance.** KHÁC update 7/12 (trước: coverage-gate chặn scan dù event tới; GIỜ: event 1d không tồn tại).

### Bằng chứng
- **DB signals**: 00:00 20/07 CHỈ intraday (15m/1h/4h, ~14 alpha) có signal; 0/6 daily. Last signal daily: iamp/kertrend/vwaprev=19/07 09:15 (late event-symbol, không phải 00:00), chmom/ensemble=18/07 02:57, trend60cmf=19/07 00:01.
- **Runner** (khỏe, health=ok, stale=none, CÓ subscribe kline:binance:1d): `last_event_ts_by_alpha` cả 6 daily đứng im `2026-07-19T11:52:38` = warmup restart → 0 event 1d suốt 15h. Đếm message theo kênh: 15m=541, 1h=136, 4h=10, **1d=0** data.
- **MDS metric** `mds_candles_processed_total`: 15m=44112,1h=11181,4h=2634,12h=1301, **1d=KHÔNG có dòng (=0)** kể từ restart.
- **MDS Redis** `kline_snapshot_v2:binance:*:BTCUSDT`: 12h tươi (close 00:00 20/07), **1d cũ** (close 00:00 19/07, `"exchange":"binance"` = từ warmup REST, không phải rollup live `exchange:""`).
- **Điểm gãy cascade** (1m→15m→1h→4h→12h→1d): snapshot **thiếu 4h@08:00 19/07** + **thiếu 12h@00:00 19/07** (+3 bản trùng 12h@12:00 18/07) = chữ ký restore KHÔNG đầy đủ khi restart giữa ngày (warmup log "1d: 6 complete, 71 insufficient"). Thiếu 4h@08:00 → 12h@[00:00–12:00 19/07] không hình thành → 1d@[00:00 19→00:00 20] cần 2 nến 12h chỉ có 1 → `_try_rollup` (aggregator.py:140,~185 `len(parts)<needed`) trả None → không sinh 1d.

### ROOT CAUSE + trigger
- **Trigger**: `.env` MDS (market-data-service/.env) sửa **11:45 UTC 19/07** (đúng deploy update-13 `BINANCE_TOP_SYMBOLS=520→0`) → restart MDS → reset buffer in-memory → restore higher-TF không dựng lại chuỗi 1d ngày 19/07.
- **Config phụ (nên sửa)**: `.env TIMEFRAMES=15m,1h,4h,12h` **BỎ `1d`** (code default config.py:33 có `1d`). Aggregator `_rollup_after_1m_close_locked` hardcode bước 12h→1d nên KHÔNG chặn trực tiếp; NHƯNG `restore_from_redis`/`restore_from_cache_publishable` lặp theo `self.timeframes`=get_timeframes() → không restore buffer 1d → chuỗi khó tự dựng sau restart. Thêm `1d` lại cho nhất quán.
- Fix funding_zscore update-14 (chmom/ensemble) vô nghĩa tới khi 1d chạy lại — cả 6 bị chặn thượng nguồn.

### Dự đoán tự lành + khuyến nghị
- **Có thể tự lành 00:00 UTC 21/07**: hôm nay MDS có đủ data live dựng lại 4h→12h→1d của ngày 20/07 (1d@[00:00 20→00:00 21] chỉ cần 12h của ngày 20, độc lập với hố ngày 19). VERIFY 00:05 21/07: `candles_processed_total{tf="1d"}` xuất hiện + snapshot 1d close 00:00 21/07 + last_event_ts 6 daily nhảy 00:00.
- **Nếu không lành**: thêm `1d` vào TIMEFRAMES + sửa restore để tái dựng higher-TF sau restart (hoặc warmup fetch trực tiếp 1d/12h). Gốc thật: restart giữa ngày KHÔNG được để thủng cascade rollup.
- **Recovery**: `scripts/recover_rebalances.py --only-alpha` bù 2 nhịp lỡ (00:00 19/07 + 20/07) cho 6 daily (như update 8).
- **Observability**: alert khi tf đã subscribe mà 0 event vượt cadence (1d>25h) — ẩn 15h vì "No silent failures" chưa phủ tầng feed 1d.

---

## 2026-07-19 (cập nhật 14) — Claude: chmom + ensemble trắng = KeyError 'funding_zscore' (bug RUNNER, không phải MDS)

Cả `1d-chmom` + `ensemble-1d` crash `KeyError: 'funding_zscore'` tại `cross_alpha/strategy.py:472` (`compute_signal_details` đọc `fields["funding_zscore"]`) mỗi khi scan. Loop bắt exception (`main.py:447`) → log ERROR → không emit signal → 2 alpha trắng.
- Chỉ lỗi ở **event symbols** (09:15 + 10:50, 2 lần) vì chmom/ensemble không scan ở daily close (coverage miss như nhóm 197). chmom signal=carry_momentum; ensemble=ensemble_mean blend member chmom. Cả 2 `needs_funding=true`.
- **NGUYÊN NHÂN gốc**: `_attach_funding_panel` (strategy.py:419) KHÔNG gắn `funding_zscore` vào panel trước khi compute đọc → KeyError.
- **ĐÃ LOẠI TRỪ**: (a) MDS funding data — đầy đủ 197/197, tươi 3h, 0 fail; (b) funding READ — repro bằng **đúng client from_url của runner** → 197/197 nonempty, build_funding_panel KHÔNG rỗng. → read hoạt động. (c) spec — chmom+ensemble đều needs_funding=true, local==server.
- → Bug nằm ở **đường attach funding vào SHARED panel** (SharedPanelFeatureCache, key `(id,tf,universe,bars,version)`): panel mà compute đọc THIẾU funding_zscore dù `_attach_funding_panel` lẽ ra mutate bundle.panel in-place. Nghi race/caching: bundle bị rebuild/swap giữa attach và compute, HOẶC một early-return im lặng trong `_attach_funding_panel` dưới ngữ cảnh chạy thật (hiện KHÔNG log nên vô hình).
- **Silent-until-scan**: fail âm thầm (`_attach_funding_panel` không log branch return + scan-exception chỉ log không alert + chmom hiếm khi scan) → ẩn lâu. Vi phạm "No silent failures".

### ROOT CAUSE CHỐT (đọc code, không đoán)
5 alpha universe-197 đều `universe_mode=dynamic_top_k size=180` → cùng mask key `("dynamic_top_k",180)`, cùng bundle key → **CHIA SẺ 1 CrossAlphaComputeContext**. `masked_fields` (cross_alpha/strategy.py:29-37) với dynamic_top_k **snapshot + memoize** `{name: value.where(mask) for name,value in panel.items()}`. Context build tại get_bundle (shared_panel_feature_cache.py:138) TRƯỚC khi `_attach_funding_panel` mutate `bundle.panel`. → alpha non-funding (iamp) compute trước memoize snapshot THIẾU funding_zscore → chmom/ensemble tái dùng snapshot cũ → `fields["funding_zscore"]` KeyError. Regression từ shared-context single-flight (b3e5b16).

### FIX đã chọn + implement
- `cross_alpha/strategy.py`: thêm `CrossAlphaComputeContext.invalidate_masked_fields()` (clear `_masked_fields`).
- `runner/.../cross_sectional/strategy.py` `_shared_panel_bundle`: sau `_attach_funding_panel`, gọi `bundle.context.invalidate_masked_fields()` → masked_fields rebuild WITH funding_zscore (masked đúng cadence). Order-independent.
- Defense: (cân nhắc) log khi funding_zscore vắng + graceful. Concurrency: shared-context vốn có risk (pre-existing) — invalidate chỉ thêm 1 rebuild, không xấu hơn.
- LƯU Ý deploy: runner có P0 changes local chưa deploy (cập nhật 6) → tách cẩn thận.

---

## 2026-07-19 (cập nhật 13) — Claude: MDS BỎ CAP top-520 → lấy TẤT CẢ symbol (fix gốc coverage)

Theo chỉ đạo đại diện: đổi `BINANCE_TOP_SYMBOLS=520 → 0` (=lấy hết TRADING USDT-perp, tự scale) trong `market-data-service/.env` (local+server), `make deploy` ~10:50 UTC 19/07.
- **Verify**: universe **520→665 symbol**, 7 WS batch (6×100+1×65), **0×429** qua startup gap-fill 665, healthy. (A1+A2+B1 giữ vững ở scale cao hơn.)
- **Vì sao fix gốc**: mọi symbol whitelist alpha giờ ĐỀU trong live-feed → fresh-candle coverage ở 00:00 đạt ~100% → daily alpha scan đúng nhịp. Bonus: churn universe ~0 (trước ±45/ngày do volume-rank) → coverage ổn định.
- **⚠️ TÁC DỤNG PHỤ (chưa xử)**: `live_tradable_symbols`=665 → cổng tradability trong `_apply_selection` (strategy.py:544) KHÔNG còn chặn → alpha OPEN được cả ~145 coin đuôi thanh khoản thấp mà whitelist chứa. Rủi ro execution/slippage (paper không phản ánh). → Cần review/trim whitelist coin quá illiquid HOẶC giữ 1 filter thanh khoản riêng. Trade thật xảy ra ở nhịp rebalance kế.
- **BỎ Ý ĐỊNH market-cap ranking** (option 3): Binance không cấp market cap; đại diện chọn hướng "lấy hết" thay vì rank vốn hóa.
- **Chứng minh**: theo dõi 00:05 UTC 20/07 — kỳ vọng ĐỦ 6 daily alpha rebalance đúng 00:00 (đặc biệt iamp/kertrend/vwaprev không còn phải chờ event symbols 09:15). chmom+ensemble nếu vẫn trắng = vấn đề funding/dependency riêng.

---

## 2026-07-19 (cập nhật 12) — Claude: XÁC NHẬN root cause = COVERAGE (429 bị loại khỏi vai trò gốc)

Daily close 00:00 UTC 19/07 (kiểm lúc 10:31 UTC):
- ✅ `1d-trend60cmf` scan 00:01:30 (longs=59). ❌ 5 alpha còn lại KHÔNG scan ở 00:00 (y hệt 18/07).
- ⚠️ `1d-iamp/1d-kertrend/1d-vwaprev` rebalance TRỄ lúc **09:15** — do event `symbols:binance` (MDS daily universe refresh, timer neo vào lúc deploy MDS 09:14 hôm qua) ép scan VÔ ĐIỀU KIỆN (bypass should_scan). Tức trễ 9h, giá cũ — không phải nhịp 00:00.
- ❌ `1d-chmom` + `ensemble-1d`: trắng cả 00:00 lẫn 09:15 (nghi funding cho chmom / dependency cho ensemble — CHƯA đào sâu).

**BẰNG CHỨNG QUYẾT ĐỊNH:** tại 00:00 UTC 19/07 MDS có **0 × 429, 0 WS reconnect, 0 gap-fill** (data sạch) — mà 5 alpha VẪN miss. → **429 KHÔNG phải nguyên nhân gốc** của việc miss rebalance. Gốc = **coverage/whitelist**: whitelist-197 chứa nhiều symbol thanh khoản thấp (ngoài/đuôi top-520 live-feed) về candle chậm trong burst 00:00 → `_candle_coverage` không vượt 0.90 trong cửa sổ event live → `should_scan=False` → không scan. `1d-trend60cmf` (137 symbol thanh khoản) vượt kịp.

Metrics 25h sau deploy: `kline_queue_backpressure_total=344` (tăng, nhưng `dropped=0`), `ws_reconnect_gap_fill_total=0` (KHÔNG reconnect kể từ deploy). A1+A2+B1 giữ 429=0 — tốt cho ổn định MDS, NHƯNG **không chữa daily rebalance** (đúng như dự đoán).

**FIX THẬT (chưa làm):** trim whitelist-197 về symbol trong top-520 live-feed (nhanh, trị gốc) HOẶC time-based retry sau daily close HOẶC hạ `scan_min_symbol_coverage`. Đây là việc phía RUNNER (paper-trade-system), tách hẳn khỏi MDS 429 work.

---

## 2026-07-18 (cập nhật 11) — Claude: C1 điều tra WS reconnect — CODE ANALYSIS xong, chờ xác nhận empirical ở 00:00 UTC 19/07

### Cơ chế reconnect (kline_feed.py:78-142) — 3 trigger
1. **silence 30s** (`silence_timeout=30`): 30s không có message → break → reconnect.
2. **BinanceWebsocketQueueOverflow**: internal WS queue (`ws_queue_maxsize=50000`) tràn → reconnect ngay.
3. **exception** khác → backoff reconnect. (Ngoài ra server-side close vd Binance 24h-limit → `async with` thoát → loop reconnect, CÓ THỂ không có log cause riêng.)

### Chuỗi tự-khuếch đại (code-supported, giả thuyết dẫn đầu)
`put_with_backpressure` (kline_queue.py) dùng `await queue.put()` → **BLOCK WS recv khi kline queue (`BINANCE_KLINE_QUEUE_MAXSIZE=10000`) đầy**. Tại **daily close 00:00**: 520 symbol đóng 1m cùng lúc + rollup ra 5m/15m/1h/4h/12h/1d → aggregation (`asyncio.to_thread` pool mặc định) bão hòa → consume_queue tụt → kline queue đầy → chặn WS recv → BinanceSocketManager internal queue (50000) tràn → **QueueOverflow → reconnect** → (mỗi batch reconnect) gap-fill → 429 → block thêm → reconnect thêm. **Tự khuếch đại.**

### Bằng chứng có/mất
- CÓ: reconnect cluster **00:31/01:02/01:10** (ngay sau daily close 00:00), TRÙNG cụm gap-fill+429 — KHÔNG trùng mốc feed-rebuild 12:09+24h → **nghịch với giả thuyết thuần Binance-24h-limit**.
- MẤT: log reconnect-cause lịch sử (silent/overflow/exception) đã bị xóa khi `--force-recreate` lúc deploy cập nhật 10. Không khôi phục được (market-data.log stale 2/7).

### Lỗ hổng instrumentation
KHÔNG có counter đếm kline WS reconnect theo cause (chỉ `depth_ws_reconnects_total` cho orderbook + `ws_reconnect_gap_fill_total` proxy). Cause chỉ ở log ephemeral. → Đề xuất (follow-up code): thêm `mds_kline_ws_reconnect_total{cause=silent|overflow|error|clean}`.

### Baseline metric (09:21 UTC, để so ở 00:00 UTC 19/07)
`kline_queue_backpressure_total=1`, `kline_queue_depth=0`, `kline_queue_dropped_total=0`, `ws_reconnect_gap_fill_total=0`, `rate_limit_waits_total=202`.

### XÁC NHẬN ở 00:05 UTC 19/07 (container fresh sẽ giữ log tới lúc đó nếu không redeploy)
```
docker logs --since 40m market-data-service-market-data-service-1 2>&1 | grep -E "silent for 30s|queue overflow|Batch [0-9]+ error|Filling gaps"
# + snapshot lại 5 counter trên; nếu backpressure_total & dropped_total & gap_fill_total CÙNG spike quanh 00:00 -> XÁC NHẬN overflow-at-daily-close.
```

### Mitigation ứng viên (theo cause — làm sau khi xác nhận)
- Nếu overflow-at-daily-close: nâng `BINANCE_KLINE_QUEUE_MAXSIZE` (10000→50000) cho headroom burst; tăng thread pool aggregation; hoặc `_queue_message` drop-oldest thay block; giãn rollup. **A2 (đã deploy) đã cắt vòng khuếch đại** (coalesce gap-fill) dù chưa giảm tần suất reconnect.
- Nếu benign 24h/server-close: chấp nhận, giảm noise (gap-fill chỉ khi gap thật — đã có), cân nhắc rotate connection chủ động.
- Universal: giảm `ws_batch_size` (100→nhỏ hơn) để mỗi reconnect ảnh hưởng ít symbol hơn.

---

## 2026-07-18 (cập nhật 10) — Claude: IMPLEMENT A1+A2+B1 (MDS 429 fix) — ĐÃ DEPLOY PRODUCTION (chưa commit, theo chỉ đạo)

**DEPLOY 2026-07-18 ~09:14 UTC** (off-peak, xa daily close): `make deploy` lên 167.86.101.228. Blast radius sạch — so hash local vs server: 7 file đổi-sẵn của repo đều SAME (server đã có), **chỉ 4 file A1+A2+B1 là DIFF** → deploy chỉ ship đúng thay đổi này. mds-redis ở compose riêng, không đụng cache.

**Verify sau deploy:** cả 2 container healthy; code A1+A2+B1 xác nhận có trong container đang chạy; **0 × 429 xuyên suốt startup gap-fill** (chính là loại mass-REST burst trước gây 429 → A1 weight-fix hiệu quả); funding poll "199 symbols" chạy qua `_poll_all` bounded, **0 fetch-fail**; không ERROR/Traceback; health=ok, kline connected. Log "gap too large — skipping" là bình thường (symbol tail stale, skip theo thiết kế).

Lưu ý: test dứt điểm A2 (coalesce reconnect) + giảm 429 bền vững sẽ hiện ở **lần WS reconnect thật kế tiếp** (episodic, không ép được) — nhưng startup gap-fill (burst tương đương) đã cho 0×429.

Chi tiết code/test bên dưới ↓

### Code A1+A2+B1 (repo market-data-service, CHƯA COMMIT)

Plan `.agents/PLAN-mds-429-fix.md` → APPROVED → đã code trong repo **market-data-service** (local, chưa commit):
- **A1** `app/adapters/binance/adapter.py`: reconnect gap-fill `weight=5` → `kline_weight(_RECONNECT_GAP_LIMIT=1500)`=10 (hết đếm thiếu 2×). Đồng bộ hằng số cho cả acquire lẫn `limit`.
- **A2** cùng file: `_on_ws_reconnect` thành guard (lock single-flight + debounce 30s) → tách body ra `_run_reconnect_gap_fill`; semaphore chuyển lên cấp instance (config `BINANCE_RECONNECT_GAP_CONCURRENCY=10`). Chặn N batch reconnect quét full-universe N×.
- **B1** `app/adapters/binance/funding_feed.py`: tách `_poll_all` bounded bằng `Semaphore(FUNDING_POLL_CONCURRENCY=15)` thay `gather` all-at-once.
- **Config** `app/config.py`: +3 knob. **Wire** `app/main.py`.
- **Test mới**: `tests/test_binance_adapter.py` +3 (weight đúng / coalesce concurrent / debounce), `tests/test_funding_feed.py` +1 (concurrency ≤ cap, poll đủ symbol).

**Verify (venv scratchpad + deps):** 22 passed (2 file đụng); slice `adapter|kline_feed|gap|funding` = **83 passed, 0 fail**; 4 test mới xanh. Ruff: 3 lỗi **pre-existing** (TF_MINUTES unused/redefine adapter.py:21/407, os unused main.py:5) — KHÔNG do PR này, không sửa (surgical).

**CẢNH BÁO commit boundary:** repo market-data-service ĐÃ có ~13 file thay đổi chưa commit từ trước (aggregator/metrics/models/proxy-router/docker-compose... — không phải của session này). Cần tách diff của A1+A2+B1 (4 file app + 2 file test + requirements nếu thêm) khi commit để PR gọn.

**Next:** (1) commit tách riêng, (2) deploy MDS off-peak (tránh 23:50–00:40 UTC), (3) sau đó chạy **C1** (điều tra WS reconnect 18×/ngày — gốc chung). Theo dõi 00:05 UTC 19/07 xem daily alpha rebalance đủ.

## 2026-07-18 (cập nhật 9) — Claude (Architect): PLAN fix 429 (A1+A2+B1) — DRAFT chờ APPROVE

Điều tra 429 xong (không mãn tính; bùng theo cụm do WS reconnect → mass gap-fill; KHÔNG do alpha v2 xin 12h). Đã dựng `.agents/PLAN-mds-429-fix.md` — 1 PR trên **market-data-service**:
- A1: sửa weight under-count `adapter.py:534` (weight=5 → kline_weight(1500)=10).
- A2: single-flight + shared semaphore cho `_on_ws_reconnect` (3 batch reconnect đang quét full-universe 3×).
- B1: bound concurrency funding poll (`funding_feed.py:96` gather-all → semaphore 15).
- C1 (điều tra WS reconnect 18×/ngày) tách riêng, chạy SAU.

STATUS: DRAFT. Chưa implement (chờ đại diện approve + trả lời 3 Unresolved Questions cuối plan). Implement = Codex/OpenCode, Claude review.

---

## 2026-07-18 (cập nhật 8) — Claude (Architect+Reviewer): RECOVERY nhịp 00:00 18/07 cho 5 daily alpha — ĐÃ PROMOTE LÊN PRODUCTION

### Kết luận
Đã feed lại nhịp rebalance lỡ `00:00 UTC 18/07` cho đúng **5 daily alpha** (`1d-kertrend, 1d-vwaprev, 1d-iamp, 1d-chmom, ensemble-1d`) vào production server `167.86.101.228` qua staged build→promote. Signal/position/trade/equity đều đã có; `1d-trend60cmf` (khỏe) không đụng tới.

### Thay đổi code (local, CHƯA COMMIT — đã scp thủ công lên server)
Tool `recover_rebalances.py` scope cứng cho sự cố 16–17 (26 alpha trong `INCIDENT_SCHEDULES`). Chạy thẳng cho 18/07 sẽ recover cả ~10 alpha intraday ĐANG KHỎE (đã rebalance đúng 00:00 18/07) → ghi đè sổ vị thế đúng của chúng. Nên thêm allowlist:
- `scripts/rebalance_recovery/scope.py`: `build_incident_points(..., only_alphas=None)` — filter schedule theo allowlist, `None`=giữ nguyên hành vi 16–17. Raise nếu alpha lạ.
- `scripts/rebalance_recovery/workflow.py`: `BuildRequest.only_alphas` + truyền vào build_incident_points + ghi vào manifest.window.
- `scripts/recover_rebalances.py`: CLI `--only-alpha` (repeatable).
- Test mới `scripts/tests/test_rebalance_recovery.py`: 3 test (allowlist giới hạn đúng 5 daily / None giữ nguyên 30 cycle / reject alpha lạ) — GREEN. Không cần filter `configs` vì `market.py` chỉ capture cho alpha có `point` (configs là superset vô hại).
- **Chỉ filter `points` là đủ**: `affected` (equity/validation) derive từ ledger←points.

### Thực thi production (đã làm)
1. Verify read-only: đúng 5 daily lỡ (signal cuối 2026-07-17T06:07, 0 signal 18/07); mọi intraday/`1d-trend60cmf` đã rebalance 00:00 18/07 → ngoài scope.
2. Tham số server: `mds_cache=/root/market-data-service/historical_cache` (đã có delta parquet 18/07 00:00 = data gap-fill về trễ), mds-redis=`localhost:6381`, paper-redis(positions)=`localhost:6382`, DB `data/paper-trade.db`+`data/equity-snapshots.db`, config `runner-config.production.yaml` (đủ 26). uv cài lại vào `~/.local/bin`.
3. Dừng `worker`+`alpha-runner`+`alpha-runner-legacy` → `build --start/--end 2026-07-18 --only-alpha`×5 → **validation PASS 10/10** (`ledger.cycles expected=5 actual=5`, `excluded.1d-trend60cmf` digest before==after, `signals=1496`, unique/duration/equity formula/total đều pass). `approval_hash=d876bcbc6bdfcf23df9bfa46352ebacca23664a7cda3b94607bdb315e78712d3`.
4. `promote --services-stopped` = COMPLETE (backup tại `recovery/incident-20260718-daily/`, append trade/signal mới + replace sổ 5 alpha + update equity rows in-place).
5. Restart writers, reconcile ghost redis = 0. **Verify:** 5 daily có 18/07 signals(258–342)/open_pos(128–168)/trades(130–174)+PnL; equity 36 row/alpha+`__TOTAL__` từ 00:00, snapshotter live đã tiếp tục tới 02:59. `1d-trend60cmf` control không đổi (242/122/120).

### Việc còn lại
- **Chưa commit** code local (3 file + test). Server đang chạy bản đã scp. Cần commit để đồng bộ repo (`feat(recovery): add --only-alpha allowlist to rebalance recovery`).
- **ROOT CAUSE vẫn chưa fix** (cập nhật 7 bên dưới): coverage-gate + edge-trigger + whitelist-197 bẩn. Recovery này chỉ vá nhịp đã lỡ; nhịp 00:00 19/07 vẫn có nguy cơ lỡ lại nếu chưa (1) trim whitelist-197, hoặc (2) thêm time-based retry sau daily close. Theo dõi 00:05 UTC 19/07.

---

## 2026-07-18 (cập nhật 7) — Claude (Architect+Reviewer): ROOT CAUSE 5 daily alpha không rebalance 00:00 18/07

### Kết luận điều tra (server live 167.86.101.228)
5 daily alpha (`1d-iamp, ensemble-1d, 1d-kertrend, 1d-chmom, 1d-vwaprev`) KHÔNG rebalance ở daily close 00:00 UTC 18/07; chỉ `1d-trend60cmf` rebalance (longs=61 shorts=61).

**KHÔNG phải** crash / hang / event-loop chết / scan timeout / mất subscription. Bằng chứng runtime:
- `/metrics`: cả 6 alpha có `last_event_ts` ~00:00:33–40 → đều XỬ LÝ event 00:00. `scan_timeout_by_alpha={}`, `stale_alphas=[]`.
- 5 alpha: 0 dòng SIGNAL_AUDIT/_apply_selection/ALPHA_TIMING(INFO) kể từ warmup → `scanned=False` → `should_scan_after_event` trả False suốt close.
- Watermark: cả 6 warmup synced về `1784160000000` (giống hệt) → loại watermark. `rebalance_bars=1`, `publish_at_midnight=None` → loại cadence.
- `strategy_readiness_coverage` HIỆN TẠI: trend60cmf=1.0, 4 con kia=0.9949 (đều > ngưỡng 0.90).

**Discriminator = whitelist**: `1d-trend60cmf` có 137 symbol; 5 con kia dùng chung whitelist 197 symbol (= 137 core + 60 symbol thanh khoản thấp, gồm AXL/MELANIA/SUPER... đã bị log "not in MDS live tradable universe").

**ROOT CAUSE**: `should_scan_after_event` (strategies/cross_sectional/strategy.py:178-209) gate scan bằng `_candle_coverage(candle_open_ms) >= scan_min_symbol_coverage(0.90)`, và **chỉ được EDGE-TRIGGER bởi event kline live**. Tại 00:00, MDS bị 429 rate-limit → candle của ~60 symbol thừa về trễ qua GAP-FILL (cập nhật cache âm thầm, KHÔNG phát runner event). Trong cửa sổ event live (kết thúc ~00:00:33) coverage trên whitelist-197 chưa đạt 0.90 → mọi should_scan=False. Khi candle trễ về (readiness lên 99.5%) thì KHÔNG có event nào re-trigger → 5 alpha bỏ lỡ nguyên cả nhịp rebalance. `1d-trend60cmf` (137 symbol thanh khoản, về đúng giờ) vượt 0.90 trong burst nên scan.

Caveat: `_candle_coverage` tại 00:00:33 không đo trực tiếp được (DEBUG bị suppress, cache live không introspect được) — bước cuối này SUY LUẬN từ chuỗi trên; xác nhận dứt điểm = bật DEBUG cho should_scan hoặc log coverage-at-decision.

`b3e5b16` (fix event-loop hang) ĐÃ deploy trên container nhưng vô can — đây là lỗi mode khác (coverage gate + edge-trigger + whitelist bẩn).

### Hướng fix (chưa làm — chờ đại diện)
1. Ngay: trim whitelist-197 về symbol thanh khoản (bỏ ~60 coin chết) → coverage vượt 0.90 trong burst.
2. Gốc: thêm time-based retry sau daily close — nếu chưa scan mà coverage đã đạt (qua gap-fill) thì re-trigger; hoặc phát runner event khi gap-fill cập nhật cache.
3. Recovery nhịp 00:00 18/07 đã lỡ cho 5 alpha = dùng `scripts/recover_rebalances.py` (restart runner KHÔNG cứu được nhịp đã lỡ).

---

## 2026-07-17 (cập nhật 6) — Claude (Architect+Reviewer): P0 reconciliation

### Đã làm
Implement P0 (plan `.agents/PLAN-p0-reconciliation.md`, STATUS APPROVED): counter đường drop signal + reconcile ledger để chứng minh "no silent failures". **LOCAL ONLY — chưa deploy server** (đại diện chốt).

- Runner: `alphas/runner/metrics.py` (+counter dispatched/dedup/lease_dropped/published, tách theo alpha_id), `signal/dispatcher.py` (wired; lease-drop WARNING→ERROR), `main.py` (truyền metrics vào dispatcher).
- Worker: `worker/app/metrics.py` MỚI (WorkerMetrics), `main.py` (đếm mọi nhánh `process_signal_message` + xack + log reconcile định kỳ), `config.py` (+RECONCILE_LOG_INTERVAL_SEC).
- Script: `scripts/reconcile_signals.py` MỚI — chạy tay, read-only, exit 1 khi gap>tolerance.
- Tests MỚI (test-first): `test_dispatcher_counters.py` (5), `test_worker_metrics.py` (7), `test_reconcile_signals.py` (8).

### Kết quả test (local)
- runner vùng liên quan 16 passed; worker full 199 passed/6 skipped; scripts 18 passed.
- 2 lỗi collection scripts (`test_recover_rebalances_cli`, `test_rebalance_recovery_capture`) = thiếu `typer`, **pre-existing**, không do P0.
- Reconcile chạy thật trên `data/paper-trade.db`: DB invariant khớp (1319 rows = 1110 committed + 209 errored); Redis local down → fail-loud exit 1.

### Known issue / next
- **209/1319 signal (~16%) đã ERRORED trong DB live** — P0 vừa phơi ra, cần điều tra (`SELECT alpha_id,error,COUNT(*) FROM signals WHERE error IS NOT NULL GROUP BY alpha_id,error`).
- Chờ đại diện: (1) chạy reconcile với Redis thật, (2) quyết định deploy, (3) có làm P1 batching không.
- Evidence: `.omo/evidence/worker-redis-u2/task-p0-summary.txt`.

## 2026-07-17 (cập nhật 5) — Codex (Recovery Operator)

### Kết quả production

Đã build, audit và promote recovery cho cửa sổ 16–17/07/2026 trên server
`167.86.101.228`. Production được merge theo từng row, không thay nguyên file DB.

- Workspace chính thức: `recovery/incident-20260716-17-stage5`.
- Manifest/approval SHA-256:
  `4a4f25bbb61a9e0d66b5526ee76529e2e54692362c5310a38e3bb47ee4b99c08`.
- Ledger: 6.268 OPEN/CLOSE events, đúng 30 cycle của 20 alpha đến lịch;
  6 alpha cadence 36h là no-op. `1d-trend60cmf` có 0 ledger event.
- Main DB baseline → candidate: positions `2683 → 3103`, trades
  `29142 → 32066`, signals `69107 → 75375`.
- Equity baseline/candidate đều 266.368 row; cập nhật 6.986 row thuộc 20 alpha
  recovery + `__TOTAL__`, không insert/delete row, 0 duplicate
  `(timestamp, alpha_id)`, 0 thay đổi trước 16/07 và 0 thay đổi
  `1d-trend60cmf`.

### Chứng cứ không mất dữ liệu

- Trước promote: 0 trade/signal cũ thiếu trong candidate; 0 position ngoài scope
  bị thêm, xóa hoặc sửa; toàn bộ artifact hash và validation check PASS.
- Promote tạo backup tại
  `promotion-backup-paper-trade.db` và
  `promotion-backup-equity-snapshots.db` bên trong workspace stage5.
- Sau promote: production khớp candidate hai chiều cho positions, trades,
  signals và equity; SQLite integrity `ok`; 20/20 Redis position keys khớp.
- Sau khi writers hoạt động lại: 0 trade/signal cũ bị thiếu, 0 equity ID cũ bị
  thiếu, 0 duplicate equity. Counts tăng bình thường do cycle live mới.

### Runtime

- Đã dừng `worker`, `alpha-runner`, `alpha-runner-legacy` trong lúc build/promote.
- Đã restart cả ba. Sau direct warmup: runner chính `27/27`, event loops chạy;
  cả ba container `healthy`, không restart/crash.
- Stage2/stage3 thất bại và QA copies tạm đã được dọn. Stage4 được giữ làm bằng
  chứng audit; stage5 và backup phải giữ để rollback nếu cần.

### Verify code

- Recovery suite: `14 passed`; Ruff PASS; basedpyright `0 errors`; strict
  no-excuse PASS.
- Row-level merge có test append history, reconcile riêng affected positions,
  equity update không đổi row count, và rollback khi production drift.

### Việc còn theo dõi

Recovery đã hoàn tất. Bằng chứng daily close kế tiếp lúc 00:00 UTC 18/07 cho sáu
daily alpha vẫn là bước observability riêng từ cập nhật 3; không ảnh hưởng tính
toàn vẹn của backfill đã promote.

## 2026-07-17 (cập nhật 4) — Codex (Primary Implementer)

### Việc đã làm
Đã viết tool recovery rebalance theo mô hình staging, không backfill trực tiếp vào production khi build và **chưa chạy command recovery**.

- Freeze scope 26 alpha, sinh đúng 30 cycle thiếu trong 16–17/07/2026; 6 alpha cadence 36h là no-op vì không đến lịch; loại `1d-trend60cmf` khỏi recovery.
- Capture input bất biến từ Parquet + Redis read-only, áp dụng coverage 90% giống runner, rồi replay bằng `cross_alpha` production logic với event time candle close + 5 giây UTC.
- Build riêng candidate main DB, trade history/open positions, Redis position state và equity DB; fill fallback 0.5 bps adverse + fee/PnL theo worker hiện tại.
- Validation bắt buộc đủ cycle, integrity, uniqueness, duration, equity formula/total và digest `1d-trend60cmf` không đổi.
- Promote tách riêng, yêu cầu manifest SHA-256, validation PASS, source watermark không drift, `--services-stopped`, backup + atomic replace + rollback; có dọn SQLite WAL/SHM.

### File chính
- `scripts/recover_rebalances.py`
- `scripts/rebalance_recovery/`
- `scripts/tests/test_rebalance_recovery*.py`
- `docs/REBALANCE_RECOVERY.md`
- `pyrightconfig.json` (thêm execution environment cho `scripts`)

### Verify cục bộ
- `6 passed` cho recovery tests trên temp SQLite.
- Ruff lint/format PASS; basedpyright core + CLI PEP 723: 0 errors; no-excuse checker PASS; `py_compile` PASS.
- Không gọi `build`, không gọi `promote`, không kết nối server/production trong giai đoạn implement.

### Bước tiếp theo
Operator đọc `docs/REBALANCE_RECOVERY.md`, dừng toàn bộ writer, chạy `build` để tạo workspace staging, review ledger/candidate/equity/validation, rồi mới quyết định có chạy `promote` bằng đúng manifest hash hay không. Root runner fix ở cập nhật 3 đã deploy; vẫn cần verify daily close 18/07 như hướng dẫn bên dưới.

---

## 2026-07-17 (cập nhật 3) — Claude (Architect)

### Việc đã làm
Commit + deploy U1-U6 lên server thật, verify healthy.

- Commit `b3e5b16` (scope `alphas/runner/` + `.agents/` — có bundle thêm 1 số việc dở dang từ trước không tách được, đại diện đã xác nhận chấp nhận).
- Deploy 04:17 UTC lên `167.86.101.228`: `make package` → scp → build + restart **chỉ** `alpha-runner` (`--no-deps`), không đụng `alpha-runner-legacy`/`worker`/core.
- Verify 04:25:58 UTC: container healthy, warmup 27/27, 6 daily alpha claim đủ (`1d-kertrend,1d-trend60cmf,1d-chmom,1d-vwaprev,1d-iamp,ensemble-1d`), không exception. `/health`→`{"status":"ok"}`, `/metrics`→`stale_alphas:[]`, `strategies_active:27`, `panel_build_total:5` (đúng cấu trúc single-flight — 27 alpha chỉ 5 nhóm build).

### Trạng thái
- **Đã deploy, cấu trúc đúng, healthy.** Nhưng **chưa xác nhận được bằng chứng cuối cùng** (6 daily alpha thực sự emit signal ở candle close) vì phải chờ tới 00:00 UTC 18/07 (~19.5h sau thời điểm deploy) — quá xa để verify trong phiên này.
- `.agents/PLAN.md` U7 đã cập nhật: phần deploy/health ĐẠT, phần "emit signal ở daily close" CHƯA XÁC NHẬN.

### Bước tiếp theo (cho phiên sau hoặc đại diện tự làm)
Sau 00:05 UTC 18/07 (giờ VN ~07:05 sáng), kiểm tra:
1. `curl http://localhost:9091/health` trên server (qua `docker compose exec -T alpha-runner ...` hoặc từ trong container) — kỳ vọng `{"status":"ok"}`, không `stale_alphas`.
2. Query DB `paper-trade.db` bảng `signals`: `select alpha_id, count(*), max(received_at) from signals where alpha_id in ('1d-kertrend','1d-trend60cmf','1d-chmom','1d-vwaprev','1d-iamp','ensemble-1d') and received_at >= '2026-07-18' group by alpha_id` — kỳ vọng cả 6 đều có ít nhất 1 signal mới (trước fix chỉ `1d-trend60cmf` có).
3. Nếu đủ 6 → đóng U7, coi sự cố đã khắc phục triệt để. Nếu thiếu alpha nào → xem log `[STRATEGY] scan timeout` (U4) hoặc exception mới để chẩn đoán tiếp.

---

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
