# Integration Plan — alphas-2 (funding-onchain, 52w-high, amihud-lowbeta)

> Phân tích + kế hoạch tích hợp 3 alpha mới từ `docs/alphas-2/` vào paper-trade-system.
> Source data: `docs/alphas-2/datacryp/` (OHLCV mẫu) + `system/datacryp/derived/` (panel features).

---

## 1. Tổng quan 3 alpha mới

| Alpha | ID | Signal (1d) | Universe | Data cần | Loại data |
|---|---|---|---|---|---|
| **funding-onchain** | N1 | `cs_zscore(ts_ema(funding_zscore21, 60)) − cs_zscore(active_users)` | ~30 coin (funding ∩ DAU) | `funding_zscore21`, `active_users` | **External** (funding API + onchain Artemis) |
| **52w-high** | N2 | `cs_zscore(ts_ema(uid, 10)) − cs_zscore(dist_high)` | ~43 coin (có 1m) | `dist_high`, `uid` | **OHLCV-derived** (dist_high từ close; uid từ 1m) |
| **amihud-lowbeta** | N3 | `cs_zscore(amihud) − cs_zscore(ts_ema(downside_beta, 90))` | ~84 coin | `amihud`, `downside_beta` | **OHLCV-derived** (cả hai từ OHLCV) |

Tất cả đều: khung **1d**, construction **winsor_cont** (magnitude sizing), dollar-neutral, gross=1.

---

## 2. Phân tích MDS — "Đủ data để chạy?"

### 2.1. MDS hiện có gì?

MDS (Market Data Service) là **real-time OHLCV gateway**:

| Component | Có | Ghi chú |
|---|---|---|
| OHLCV 1m → aggregate 1d | ✅ | Binance, OKX, KuCoin, BingX, Bybit |
| Redis Pub/Sub + snapshot + warm-up | ✅ | Alpha consume qua `kline:{tf}` channel |
| Price alerts | ✅ | TP/SL tracking |
| **Funding rate** | ❌ | Hyperliquid adapter chỉ lấy kline, KHÔNG lấy funding |
| **Onchain (active_users, TVL, fees)** | ❌ | Không có adapter onchain |
| **Derived features (amihud, dist_high, uid, downside_beta...)** | ❌ | Alpha phải tự compute hoặc load từ file |

### 2.2. Gap analysis từng feature

| Feature | Nguồn | MDS có? | Cách lấy | Ghi chú |
|---|---|---|---|---|
| `dist_high` | `close / rolling_max(close, W) - 1` | ✅ Từ OHLCV | Compute on-the-fly từ panel `close` | Đơn giản, ~5 dòng code |
| `uid` | Entropy phân bố thông tin nội ngày từ 1m data | ✅ Từ 1m OHLCV | Compute từ 1m panel (cần 1m warm-up) | Phức tạo hơn; chỉ ~102 coin có đủ 1m history |
| `amihud` | `mean(|return| / quote_volume)` | ✅ Từ OHLCV | Đã có signal `amihud` trong cross_alpha | Nhưng alpha doc reference panel pre-computed → cần decide |
| `downside_beta` | Regression coin returns vs market returns (down days) | ✅ Từ OHLCV | Compute từ `close` panel + market index | Cần market equal-weight index |
| `funding_zscore21` | `ts_zscore(funding_rate, 21)` | ❌ KHÔNG có | Cần funding rate feed từ CEX API | Binance/OKX funding API (8h interval) |
| `active_users` | Artemis API (daily) | ❌ KHÔNG có | Cần onchain data feed | Free API, 53 coin, daily refresh |

### 2.3. Kết luận MDS sufficiency

| Alpha | MDS đủ? | Lý do |
|---|---|---|
| **52w-high** | ⚠️ Tạm đủ | `dist_high` OK; `uid` cần 1m data (MDS có 1m nhưng chỉ cho ~102 coin, không phải 200) |
| **amihud-lowbeta** | ✅ Đủ | Cả `amihud` và `downside_beta` compute được từ OHLCV 1d |
| **funding-onchain** | ❌ KHÔNG đủ | Cần `funding_zscore21` (funding rate) + `active_users` (onchain) — MDS không có |

**MDS cần mở rộng** để chạy được `funding-onchain`. Hai alpha còn lại có thể chạy với MDS hiện tại + mở rộng CrossSectionalEngine.

---

## 3. Kiến trúc tích hợp

### 3.1. Vấn đề cốt lõi

`CrossSectionalEngine` hiện tại:
- `build_panel()` chỉ tạo `{close, high, low, volume, quote_volume, vwap}` từ MDS snapshot
- `compute_signal_details()` chỉ support signals tính từ OHLCV fields (zscore, momentum, amihud, breakout...)
- **Không có mechanism load external/derived panel data** (funding, onchain, pre-computed features)

→ Cần mở rộng engine để panel có thể chứa **bất kỳ field nào** (không chỉ OHLCV).

### 3.2. Kiến trúc đề xuất — 3 layer

```
┌─────────────────────────────────────────────────────┐
│  Layer 1: Data Sources                              │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ MDS      │  │ Derived      │  │ External      │  │
│  │ (OHLCV)  │  │ Parquet      │  │ Fetcher       │  │
│  │ real-time│  │ (static)     │  │ (daily batch) │  │
│  └────┬─────┘  └──────┬───────┘  └───────┬───────┘  │
│       │               │                  │          │
│  Layer 2: Panel Builder                              │
│  ┌────▼───────────────▼──────────────────▼───────┐  │
│  │  Panel: {close, volume, funding_zscore21,     │  │
│  │   active_users, dist_high, uid, amihud,       │  │
│  │   downside_beta, ...}                          │  │
│  │  Mỗi field = DataFrame[time × symbols]        │  │
│  └───────────────────────┬───────────────────────┘  │
│                          │                           │
│  Layer 3: Signal Engine                              │
│  ┌───────────────────────▼───────────────────────┐  │
│  │  compute_signal_details(panel, spec)           │  │
│  │  - New signals: funding_onchain, 52w_high,    │  │
│  │    amihud_lowbeta                             │  │
│  │  - Hoặc: generic expression evaluator         │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### 3.3. Data pipeline theo loại

| Loại | Pipeline | Refresh | Latency |
|---|---|---|---|
| OHLCV (MDS) | Real-time Redis Pub/Sub + warm-up | Real-time | <1s |
| OHLCV-derived (dist_high, amihud, downside_beta, uid) | Compute on-the-fly trong alpha | Mỗi bar close | ~0s |
| External static (funding, onchain) | Load parquet tại startup + daily reload | Daily (00:00 UTC) | ~1 ngày |
| External real-time (funding rate) | Batch fetch từ CEX API, cập nhật parquet | 8h (funding interval) | ~8h |

---

## 4. Plan chi tiết — 4 Phase

### Phase 1: Mở rộng CrossSectionalEngine — Derived Panel Loader

**Mục tiêu:** Cho phép panel chứa field ngoài OHLCV (funding, onchain, pre-computed features).

**Thay đổi:**

1. **`alphas/cross_alpha/derived_loader.py`** (NEW)
   - Class `DerivedPanelLoader`: load parquet panel files → `dict[str, pd.DataFrame]`
   - Config: list of `(field_name, parquet_path, timeframe)` tuples
   - Align index với OHLCV panel (reindex + forward-fill)
   - Daily reload mechanism (reload at 00:00 UTC bar close)

2. **`alphas/cross_alpha/engine.py`** (MODIFY)
   - Trong `_process_latest_candle()`: merge derived fields vào panel trước khi gọi `compute_signal_details()`
   - Add config: `DERIVED_DATA_DIR`, `DERIVED_FIELDS` (list of field specs)

3. **`alphas/cross_alpha/strategy.py`** (MODIFY)
   - `build_panel()`: accept optional `derived_fields` param, merge vào panel dict
   - `compute_signal_details()`: add `field` lookup cho bất kỳ field name nào (không chỉ close/high/low/volume)
   - Add `ts_ema` (EMA time-series) vào `CrossAlphaComputeContext` — chưa có

4. **`alphas/cross_alpha/spec.py`** (MODIFY)
   - Add signal types: `funding_onchain`, `52w_high`, `amihud_lowbeta`
   - Hoặc: generic `expression` signal type (parse formula string → compute)

**Files affected:**
```
alphas/cross_alpha/
  derived_loader.py    (NEW)
  engine.py            (MODIFY — merge derived vào panel)
  strategy.py          (MODIFY — new signals + ts_ema + generic field lookup)
  spec.py              (MODIFY — new signal types)
  tests/               (ADD — test new signals)
```

**Acceptance:**
- `build_panel()` trả về dict có cả OHLCV + derived fields
- `compute_signal_details()` nhận field name bất kỳ, không crash nếu field thiếu
- Unit test: load 1 parquet file → merge → compute signal → verify shape

---

### Phase 2: Implement 3 alpha mới

Mỗi alpha = 1 thư mục dưới `alphas/`, inherit `CrossSectionalEngine`, config riêng.

#### 2.1. `alphas/funding-onchain/`

```json
// spec.json
{
  "alpha_id": "funding-onchain",
  "timeframe": "1d",
  "signal": "funding_onchain",
  "params": {
    "funding_ema_window": 60,
    "funding_field": "funding_zscore21",
    "onchain_field": "active_users"
  },
  "universe_size": 30,
  "universe_mode": "dynamic_top_k",
  "rebalance_bars": 1,
  "exec_lag": 0,
  "vol_lookback": 30,
  "ppy": 365,
  "long_threshold": null,
  "short_threshold": null,
  "target_vol": 0.1,
  "max_leverage": 3.0,
  "fee_bps": 7.0,
  "construction": "winsor_cont",
  "winsor_k": 3.0
}
```

```python
# app/config.py
class AlphaConfig(BaseConfig):
    ALPHA_ID: str = "funding-onchain"
    TF: str = "1d"
    WARMUP_BARS: int = 500
    DATA_MAX_CANDLES: int = 500
    MAX_CONCURRENT_POSITIONS: int = 30
    SPEC_FILE: str = str(ROOT / "spec.json")
    UNIVERSE_FILE: str = str(ROOT / "data" / "universe.json")
    DERIVED_DATA_DIR: str = "/data/derived"  # mount từ datacryp/derived
    DERIVED_FIELDS: str = "funding_zscore21:funding_termstructure/funding_zscore21,active_users:onchain_broad/active_users"
```

```python
# app/engine.py
from app.config import settings
from cross_alpha.engine import CrossSectionalEngine

class AlphaEngine(CrossSectionalEngine):
    def __init__(self):
        super().__init__(settings)
```

**Data cần mount:**
- `datacryp/derived/funding_termstructure/funding_zscore21.parquet`
- `datacryp/derived/onchain_broad/active_users.parquet`

#### 2.2. `alphas/52w-high/`

```json
// spec.json
{
  "alpha_id": "52w-high",
  "timeframe": "1d",
  "signal": "52w_high",
  "params": {
    "uid_ema_window": 10,
    "dist_high_window": 252,
    "uid_field": "uid",
    "dist_high_field": "dist_high"
  },
  "universe_size": 43,
  ...
}
```

**Data cần mount:**
- `datacryp/derived/hifreq/1d/uid.parquet` (hoặc compute on-the-fly từ 1m)
- `datacryp/derived/pricepath/1d/dist_high.parquet` (hoặc compute from close)

**Decision point:** `dist_high` có thể compute on-the-fly từ `close` (5 dòng code). `uid` phức tạp hơn — nên load pre-computed parquet.

#### 2.3. `alphas/amihud-lowbeta/`

```json
// spec.json
{
  "alpha_id": "amihud-lowbeta",
  "timeframe": "1d",
  "signal": "amihud_lowbeta",
  "params": {
    "amihud_window": 1,
    "downside_beta_ema_window": 90,
    "market_index": "equal_weight"
  },
  "universe_size": 84,
  ...
}
```

**Data cần:** KHÔNG cần external data. Compute on-the-fly:
- `amihud`: đã có signal `amihud` trong cross_alpha (compute từ `|return| / quote_volume`)
- `downside_beta`: cần add mới — regression of coin returns vs market returns on down-market days

---

### Phase 3: External Data Pipeline (funding + onchain)

**Mục tiêu:** Daily batch fetch + update parquet files cho `funding_zscore21` và `active_users`.

#### 3.1. Funding Rate Fetcher

```
scripts/fetch_funding.py  (NEW)
```

- Fetch funding rate từ Binance USD-M API: `GET /fapi/v1/fundingRate?symbol=BTCUSDT&startTime=...&endTime=...`
- Interval: 8h (3 lần/ngày)
- Symbols: top-200 từ universe.json
- Output: append vào `datacryp/derived/funding_termstructure/funding_raw.parquet`
- Post-process: compute `funding_zscore21 = ts_zscore(funding_raw, 21)`
- Schedule: cron job chạy mỗi 8h (hoặc daily lúc 00:00 UTC)

**Alternative:** Add funding rate adapter vào MDS (lớn hơn, cho real-time). Chỉ cần nếu muốn real-time funding signal. Cho daily alpha, batch fetch là đủ.

#### 3.2. Onchain Active Users Fetcher

```
scripts/fetch_onchain.py  (NEW)
```

- Fetch active_users từ Artemis API: `GET https://api.artemis.fyi/v1/developers/{asset}/daily-active-addresses`
- Symbols: ~53 coins có blockchain riêng (list trong `onchain_broad/_coverage.csv`)
- Output: append vào `datacryp/derived/onchain_broad/active_users.parquet`
- Schedule: cron job daily lúc 00:30 UTC

**Caveat từ README_onchain.md:**
- Không point-in-time (Artemis trả series bản-mới-nhất) → cần snapshot-and-freeze mỗi ngày
- Memecoin/CEX-token/perp-only ~35-50% universe không có data on-chain → universe giảm xuống ~30 coin

#### 3.3. Docker Compose integration

```yaml
# alphas/funding-onchain/docker-compose.yml
services:
  alpha:
    volumes:
      - ./data:/data
      - /path/to/datacryp/derived:/data/derived:ro  # mount derived parquet
    environment:
      - DERIVED_DATA_DIR=/data/derived
      - DERIVED_FIELDS=funding_zscore21:funding_termstructure/funding_zscore21,active_users:onchain_broad/active_users
```

---

### Phase 4: Testing & Validation

1. **Unit tests:** Mỗi signal mới có test case với sample data
2. **Backtest validation:** Chạy signal trên historical parquet → verify Sharpe/OS/WFE khớp với alpha doc
3. **Paper trade forward:** Chạy alpha live trong 1-2 tuần → verify signal ổn định, không crash
4. **Coverage check:** Verify universe size thực tế (30/43/84 coin) khớp với alpha doc

---

## 5. Thứ tự thực thi (Dependency)

```
Phase 1 (Engine mở rộng)
  │
  ├──► Phase 2.3 (amihud-lowbeta)     — không cần external data, test ngay
  ├──► Phase 2.2 (52w-high)           — cần uid parquet (đã có)
  └──► Phase 3 (External pipeline) ──► Phase 2.1 (funding-onchain)
                                        cần funding + onchain data pipeline
```

**Recommend:**
1. Phase 1 trước (engine mở rộng)
2. Phase 2.3 (amihud-lowbeta) — nhanh nhất, không cần external data
3. Phase 2.2 (52w-high) — cần load uid parquet
4. Phase 3 + Phase 2.1 (funding-onchain) — cần data pipeline riêng

---

## 6. Risk & Caveat

| Risk | Impact | Mitigation |
|---|---|---|
| Onchain data không point-in-time | Look-ahead bias | Snapshot-and-freeze mỗi ngày; ghi chú trong alpha doc |
| Universe mỏng (30 coin cho funding-onchain) | Breadth thấp, ước lượng biến động cao | Walk-forward validation; cluster-neutral check |
| Funding rate history ngắn (2023-07+) | IS window không đủ 3 năm | Walk-forward thay vì IS/OS split |
| `uid` chỉ có 102 coin | Universe 52w-high bị giới hạn 43 coin | Accept; hoặc compute uid on-the-fly từ 1m (cần thêm code) |
| Parquet reload daily có thể chậm | Alpha downtime | Reload trong off-hours (00:00 UTC); dùng lazy load |
| MDS không có funding rate real-time | Không thể real-time funding signal | Batch fetch 8h là đủ cho daily alpha; real-time nếu cần sau |

---

## 7. Tóm tắt

| Câu hỏi | Trả lời |
|---|---|
| **MDS đủ data chạy 3 alpha?** | 2/3 đủ (52w-high, amihud-lowbeta). `funding-onchain` KHÔNG đủ — cần thêm funding rate + onchain data pipeline. |
| **Cần mở rộng gì?** | (1) CrossSectionalEngine: load derived parquet vào panel + new signal types. (2) External fetcher scripts cho funding + onchain. (3) 3 alpha directories mới. |
| **Effort ước tính?** | Phase 1: 2-3 ngày. Phase 2: 1-2 ngày/alpha. Phase 3: 2-3 ngày. Tổng ~8-12 ngày. |
| **Alpha nào dễ nhất?** | `amihud-lowbeta` — pure OHLCV, không cần external data. |
| **Alpha nào khó nhất?** | `funding-onchain` — cần 2 external data source (funding + onchain). |
