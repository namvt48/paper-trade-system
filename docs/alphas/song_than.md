# Song Than

---

## 0. Tổng quan chiến lược

Song Than là chiến lược **zone-based mean-reversion trên Futures**. Ý tưởng cốt lõi:

1. Xác định hai vùng giá có ý nghĩa cấu trúc: **Green Zone** (support/discount) và **Red Zone** (resistance/premium).
2. Đặt lệnh LIMIT ở biên của từng vùng, chờ giá chạm vào và bật lại.
3. Quản lý rủi ro bằng SL cố định + trailing stop 2 cấp.
4. Đảo chiều bằng MARKET order khi thua SL liên tiếp (mean-reversion on losses).

---

## 1. Dữ liệu đầu vào

| Trường | Kiểu | Mô tả |
|--------|------|-------|
| `open_time` | int (ms) | Timestamp mở nến (Unix ms) |
| `open` | float | Giá mở |
| `high` | float | Giá cao nhất |
| `low` | float | Giá thấp nhất |
| `close` | float | Giá đóng |

**Timeframe gốc:** 1m (bot stream từ Binance WebSocket `@kline_1m`).
**Timeframe tính zone:** resample lên `ZONE_TF = 15m` trước khi tính toán.

> Researcher có thể bỏ qua bước resample nếu tải thẳng data 15m từ exchange.

---

## 2. Tính toán Zones

Zones được tính trên chuỗi nến 15m. Kết quả là hai vùng giá cho **bar hiện tại**:

```
green_low  < green_high  < red_low  < red_high
```

### Pipeline

```
OHLCV 15m  →  Swing Points  →  Trailing Levels  →  Zones (Green / Red)
```

---

### 2.1 Swing Points

**Input:** mảng `high[0..N-1]`, `low[0..N-1]`  
**Parameter:** `L = SWING_LENGTH = 50`  
**Output:** mảng `swing_high[0..N-1]`, `swing_low[0..N-1]` (phần lớn là NaN)

**Thuật toán** (port từ LuxAlgo SMC PineScript):

```python
def compute_swing_points(high, low, L=50):
    N = len(high)
    swing_high = [NaN] * N
    swing_low  = [NaN] * N
    leg = 0  # 0 = Bearish, 1 = Bullish  (khởi tạo Bearish)

    for i in range(L, N):
        p = i - L  # pivot candidate index

        # Cửa sổ so sánh: bars (p+1) đến i (tổng cộng L bars)
        window_max_high = max(high[p+1 : i+1])
        window_min_low  = min(low[p+1  : i+1])

        is_pivot_high = high[p] > window_max_high
        is_pivot_low  = low[p]  < window_min_low

        prev_leg = leg
        if is_pivot_high:   leg = 0   # bearish leg
        elif is_pivot_low:  leg = 1   # bullish leg

        # Chỉ đánh dấu khi leg ĐỔI CHIỀU
        if leg != prev_leg:
            if leg == 0:  swing_high[p] = high[p]   # đỉnh xác lập bearish leg
            else:         swing_low[p]  = low[p]    # đáy xác lập bullish leg

    return swing_high, swing_low
```

**Lưu ý quan trọng cho backtest:**

- Bar `p = i - L` được đánh dấu tại thời điểm bar `i` — có **độ trễ L bars** so với thực tế.
- Nếu cả `is_pivot_high` và `is_pivot_low` đều True cùng lúc (hiếm gặp), `is_pivot_high` được ưu tiên.
- `leg` state được duy trì liên tục qua toàn bộ chuỗi (không reset theo từng segment).

**Ví dụ (L=3 để dễ hiểu):**

```
Index:      0    1    2    3    4    5    6    7    8
High:     100  102  108  106  104  103  105  112  110
Low:       98  100  104  103  101   99  101  108  107
leg:        B    B    B    B    B    B   B→U   U    U
                             swing_high[2]=108    (tại i=5, p=2)
                                         swing_low[4]=101 (tại i=7, p=4)
                                                           ↑ chưa xác nhận tại bar 5
                                                             vì leg chưa đổi
```

---

### 2.2 Trailing Levels

**Input:** `swing_high`, `swing_low`, `high`, `low` (từ bước 2.1)  
**Parameter:** `L = 50`  
**Output:** `trail_up[0..N-1]`, `trail_dn[0..N-1]`

`trail_up` là mức giá trần đang theo dõi trong leg hiện tại.  
`trail_dn` là mức giá sàn đang theo dõi trong leg hiện tại.

```python
def compute_trailing_levels(swing_high, swing_low, high, low, L=50):
    N = len(high)
    trail_up = [NaN] * N
    trail_dn = [NaN] * N

    leg = 0            # 0 = Bearish, 1 = Bullish
    cur_up = -inf
    cur_dn = +inf

    for i in range(N):
        # Tại bar i: kiểm tra swing tại p = i - L (nếu đủ bars)
        if i >= L:
            p = i - L
            if swing_high[p] is not NaN and leg != 0:
                # Leg vừa CHUYỂN sang Bearish → reset trail_up
                cur_up = swing_high[p]
                leg = 0
            if swing_low[p] is not NaN and leg != 1:
                # Leg vừa CHUYỂN sang Bullish → reset trail_dn
                cur_dn = swing_low[p]
                leg = 1

        # Expand: trail_up chỉ tăng, trail_dn chỉ giảm
        cur_up = max(cur_up, high[i])
        cur_dn = min(cur_dn, low[i])

        trail_up[i] = cur_up
        trail_dn[i] = cur_dn

    return trail_up, trail_dn
```

**Quy tắc expand vs reset:**

| Sự kiện | trail_up | trail_dn |
|---------|----------|----------|
| Leg chuyển → Bearish (swing_high xác nhận) | Reset = swing_high[p] | Không đổi |
| Leg chuyển → Bullish (swing_low xác nhận) | Không đổi | Reset = swing_low[p] |
| Mọi bar khác | `max(cur_up, high[i])` | `min(cur_dn, low[i])` |

**Điểm dễ nhầm:** Reset xảy ra **trước** bước expand trong cùng một bar.  
Thứ tự: `(1) kiểm tra swing tại p` → `(2) reset nếu cần` → `(3) expand với bar i`.

---

### 2.3 Zones

**Input:** `trail_up[i]`, `trail_dn[i]` tại bar hiện tại  
**Output:** 4 giá trị `green_low`, `green_high`, `red_low`, `red_high`

```python
def compute_zones(trail_up, trail_dn):
    # Dải tổng thể
    rng = trail_up - trail_dn          # range = 100%

    # Red Zone: top 5% của dải (kháng cự / premium)
    red_high   = trail_up
    red_low    = trail_up * 0.95 + trail_dn * 0.05

    # Green Zone: bottom 5% của dải (hỗ trợ / discount)
    green_high = trail_dn * 0.95 + trail_up * 0.05
    green_low  = trail_dn

    return green_low, green_high, red_low, red_high
```

**Minh họa** với `trail_dn = 90`, `trail_up = 110` (range = 20):

```
  110.0  ┤══════════════════╗  red_high
  109.0  ┤  RED ZONE        ║  red_low  = 110×0.95 + 90×0.05
         │  (kháng cự)      ║
         │                  ║
         │  ...Equilibrium. ║  (90% giữa, không giao dịch)
         │                  ║
   91.0  ┤  GREEN ZONE      ║  green_high = 90×0.95 + 110×0.05
   90.0  ┤══════════════════╝  green_low
```

> `red_low = 110×0.95 + 90×0.05 = 104.5 + 4.5 = 109`  
> `green_high = 90×0.95 + 110×0.05 = 85.5 + 5.5 = 91`

**Điều kiện hợp lệ:** `trail_up > trail_dn`. Bỏ qua (không tính zone) nếu vi phạm.

---

## 3. Tín hiệu Entry

Tín hiệu được sinh ra **sau khi bar 15m đóng** (dùng OHLC của bar đó).

```python
def get_entry_signals(bar, green_high, red_low):
    signals = []

    if bar.low <= green_high:
        signals.append({
            "side":        "LONG",
            "limit_price": green_high,   # đặt LIMIT tại biên trên green zone
        })

    if bar.high >= red_low:
        signals.append({
            "side":        "SHORT",
            "limit_price": red_low,      # đặt LIMIT tại biên dưới red zone
        })

    return signals  # có thể trả về [], [LONG], [SHORT], hoặc [LONG, SHORT]
```

**Lưu ý backtest:**

- Tín hiệu sinh ra lúc bar `t` đóng → lệnh LIMIT được "đặt" vào đầu bar `t+1`.
- Lệnh fill khi `bar[t+1].low <= limit_price` (LONG) hoặc `bar[t+1].high >= limit_price` (SHORT).
- Giả định fill tại đúng `limit_price` (không có slippage trong paper backtest).
- Nếu cả LONG và SHORT đều trigger cùng bar → **chỉ lấy lệnh được fill trước** (thực tế không thể biết, chọn một hoặc bỏ qua bar đó).

---

## 4. Quản lý vị thế

### 4.1 SL và TP khởi tạo

Tính ngay sau khi lệnh fill tại `entry_price`:

```python
# LONG
sl = entry_price * (1 - SL_LONG_PCT)    # default: 1 - 0.007 = 0.993
tp = entry_price * (1 + TP_PCT)         # default: 1 + 0.02  = 1.02

# SHORT
sl = entry_price * (1 + SL_SHORT_PCT)   # default: 1 + 0.009 = 1.009
tp = entry_price * (1 - TP_PCT)         # default: 1 - 0.02  = 0.98
```

### 4.2 Trailing Stop (2 milestones)

Mỗi bar trong khi đang giữ vị thế, kiểm tra xem có đạt milestone chưa:

```python
def get_trailing_milestone(entry_price, side, bar_high, bar_low):
    if side == "LONG":
        pnl_pct = (bar_high - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - bar_low) / entry_price

    if   pnl_pct >= TRAIL_M2_PCT:  return 2   # >= 1.85%
    elif pnl_pct >= TRAIL_M1_PCT:  return 1   # >= 1.25%
    else:                          return 0
```

Khi milestone tăng lên (không bao giờ giảm), cập nhật SL:

```python
def update_trailing_sl(entry_price, side, milestone):
    if milestone >= 2:
        sl_offset = TRAIL_M2_SL_PCT   # 0.005 (0.5%)
    elif milestone >= 1:
        sl_offset = TRAIL_M1_SL_PCT   # 0.001 (0.1%)
    else:
        return None

    if side == "LONG":
        return entry_price * (1 + sl_offset)
    else:
        return entry_price * (1 - sl_offset)
```

**Quy tắc:** SL chỉ được **nới lên** (LONG: tăng) hoặc **nới xuống** (SHORT: giảm), không bao giờ đi ngược.

### 4.3 Bảng tổng hợp exit conditions

Kiểm tra theo **thứ tự ưu tiên** trong mỗi bar:

| Ưu tiên | Điều kiện | Hành động | Exit reason |
|---------|-----------|-----------|-------------|
| 1 | `bar.low <= sl` (LONG) hoặc `bar.high >= sl` (SHORT) | Đóng lệnh tại `sl` | `SL` |
| 2 | Milestone tăng lên | Cập nhật `sl` mới, tiếp tục | — |
| 3 | Milestone >= 2 | Đóng lệnh tại `close` của bar | `TP_TRAIL` |
| 4 | `bar.high >= tp` (LONG) hoặc `bar.low <= tp` (SHORT) | Đóng lệnh tại `tp` | `TP` |

> Trong production, TP được đặt bằng lệnh `TAKE_PROFIT_MARKET` trên exchange. Trong backtest, mô phỏng bằng kiểm tra giá theo bar.

---

## 5. Cơ chế Reverse Entry

### Mục đích

Sau nhiều lần SL liên tiếp trên cùng một phía, bot suy luận rằng xu hướng đang đi ngược lại → đảo chiều bằng MARKET order.

### Điều kiện kích hoạt

```python
if consecutive_sl_count >= REVERSE_SL_COUNT:   # default = 3
    # consecutive_sl_count tăng khi SL cùng phía liên tiếp
    # reset về 0 nếu SL đổi phía, hoặc khi reverse entry thực hiện
    reverse_side = "LONG" if last_sl_side == "SHORT" else "SHORT"
    # entry MARKET tại giá hiện tại
```

### SL/TP của reverse order (khác normal order)

```python
# LONG reverse
sl = entry_price * (1 - REVERSE_SL_PCT)    # default: 1 - 0.0175 = 0.9825
tp = entry_price * (1 + REVERSE_TP_PCT)    # default: 1 + 0.025  = 1.025

# SHORT reverse
sl = entry_price * (1 + REVERSE_SL_PCT)
tp = entry_price * (1 - REVERSE_TP_PCT)
```

### Cập nhật bộ đếm SL

```python
# Sau mỗi lần đóng lệnh bằng SL:
if exit_side == last_sl_side:
    consecutive_sl_count += 1
else:
    consecutive_sl_count = 1
    last_sl_side = exit_side

# Sau khi reverse entry fill (dù win hay loss):
consecutive_sl_count = 0
last_sl_side = None
```
---

## 7. Tham số mặc định

| Tham số | Ký hiệu | Giá trị mặc định |
|---------|---------|-----------------|
| Swing length | `L` | `50` bars (trên ZONE_TF) |
| Zone timeframe | `ZONE_TF` | `15m` |
| Vốn mỗi lệnh | `INVEST_PCT` | `4.5%` balance |
| Đòn bẩy | `LEVERAGE` | `50x` |
| SL Long | `SL_LONG_PCT` | `0.7%` |
| SL Short | `SL_SHORT_PCT` | `0.9%` |
| Take profit | `TP_PCT` | `2.0%` |
| Trailing milestone 1 ngưỡng | `TRAIL_M1_PCT` | `1.25%` |
| Trailing milestone 1 SL offset | `TRAIL_M1_SL_PCT` | `0.1%` |
| Trailing milestone 2 ngưỡng | `TRAIL_M2_PCT` | `1.85%` |
| Trailing milestone 2 SL offset | `TRAIL_M2_SL_PCT` | `0.5%` |
| Số SL liên tiếp → reverse | `REVERSE_SL_COUNT` | `3` |
| Reverse SL | `REVERSE_SL_PCT` | `1.75%` |
| Reverse TP | `REVERSE_TP_PCT` | `2.5%` |
