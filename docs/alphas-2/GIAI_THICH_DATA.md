
## 2. Các "thước đo" tính ra từ giá

Từ nến giá, ta tính thêm hàng chục thước đo, mỗi cái soi một khía cạnh khác nhau. Nhóm lại:

### 2.1. Xu hướng (trend / momentum)
Coin đang lên hay xuống mạnh? Như đo "đà" của một quả bóng đang lăn.
→ `dùng giá close so với trung bình quá khứ`.

### 2.2. Biến động (volatility)
Giá nhảy nhiều hay êm? Như đo "độ rung lắc". Coin rung mạnh = rủi ro cao.
📁 `ohlc_vol/` (parkinson, garman_klass...), `realized/` (đo từ data 1 phút, chính xác hơn).

### 2.3. Thanh khoản — DỄ hay KHÓ mua bán (rất quan trọng)
Coin lớn (BTC) mua bán dễ, giá không nhúc nhích. Coin nhỏ mua một ít là giá nhảy.
**Amihud** = đo "mua $1 thì giá nhúc nhích bao nhiêu". Coin khó mua bán thường được thưởng lời cao hơn.
📁 `liquidity/`: amihud, kyle_lambda, spread_ar... → đây là một trong những nhóm tìm ra alpha tốt nhất.

### 2.4. Lượng–giá (quan hệ giữa khối lượng và giá)
Giá tăng mà khối lượng tăng theo = thật. Giá tăng mà không ai mua = đáng ngờ.
📁 `pricevol/`: pv_corr (tương quan giá-khối lượng), cmf, vwap_dev.

### 2.5. Đường đi của giá (price path)
- **dist_high** = giá hiện tại cách ĐỈNH cao nhất gần đây bao xa. Gần đỉnh = đang khỏe ("52-week-high").
- **kaufman_er** = chất lượng xu hướng (đi thẳng hay zigzag).
📁 `pricepath/`.

### 2.6. Rủi ro & "đuôi" (tail / moments)
- **iskew** = giá coin này hay có cú nhảy lên hay rớt bất ngờ (độ lệch).
- **ivol** = mức dao động riêng của coin (bỏ phần chung với thị trường).
- **downside_beta** = coin này rớt mạnh cỡ nào khi cả thị trường rớt (đo phòng thủ).
- **max_ret** = cú tăng lớn nhất gần đây (hiệu ứng "vé số" — coin từng tăng sốc).
📁 `tail/`.

### 2.7. Cao tần — tính từ data 1 phút
- **jump_ratio** = bao nhiêu % biến động là "cú nhảy" đột ngột (vs trơn tru).
- **uid** = thông tin trong ngày rải đều hay dồn cục.
- **smart_money_q** = dấu vết "tiền thông minh" (lệnh lớn của tay to).
📁 `hifreq/`.

### 2.8. "Residual" — bỏ ảnh hưởng của Bitcoin
Mọi coin đều bị BTC kéo theo. **Residual** = phần biến động RIÊNG của coin sau khi trừ đi phần "đu theo BTC".
Giúp tìm coin tự mạnh chứ không phải chỉ ăn theo thị trường.
📁 `residual/`, `btc_factor/`.

### 2.9. Nến "tái nhịp" (dollar/volume bars)
Thay vì cắt nến theo THỜI GIAN (mỗi giờ 1 nến), cắt theo **lượng tiền giao dịch** (cứ đủ $X thì 1 nến).
Cách nhìn khác → ra tín hiệu khác.
📁 `dollar_bars/`, `volume_bars/`.

### 2.10. Bối cảnh thị trường (regime / cluster / seasonality)
- **regime** = thị trường đang khỏe hay yếu (bao nhiêu % coin đang tăng).
- **clusters** = nhóm các coin hay đi cùng nhau (như "ngành").
- **seasonality** = giờ/thứ nào trong tuần coin hay tăng.
📁 `regime/`, `clusters/`, `seasonality/`.

---

## 3. Dữ liệu ĐẶC BIỆT — KHÔNG phải giá (quý nhất)

Đây là phần khác biệt, vì hầu hết người ta chỉ dùng giá. Ba nguồn:

### 3.1. Funding rate (phí phái sinh) — `funding_*`
Trên sàn phái sinh (future), người đặt cược "giá lên" phải **trả phí định kỳ** cho người cược "giá xuống"
(hoặc ngược lại), tùy bên nào đông hơn. Phí này = **funding rate**.
- Funding cao = quá nhiều người đang đặt cược giá lên → đám đông tham lam → thường sắp đảo chiều.
- Đây là **tâm lý đám đông phái sinh**, không nhìn thấy qua giá.
📁 `funding_termstructure/` (200 coin, lịch sử đầy đủ), `funding_xvenue/`, `edge/funding_*`.

### 3.2. On-chain — "sức khỏe thật" của mạng lưới — `onchain_broad/`, `fundamentals/`
Mỗi blockchain ghi lại MỌI hoạt động thật. Ta lấy:
- **active_users (DAU)** = số ví hoạt động mỗi ngày (mạng đông người dùng = khỏe thật).
- **transactions** = số giao dịch.
- **fees_usd / revenue_usd** = phí & doanh thu mạng thu được (như "doanh thu công ty").
- **active_addr, tx_cnt, mcap** = ví hoạt động, số giao dịch, vốn hóa.
→ Đây là **giá trị nền tảng**, không phải đầu cơ giá. Coin có nhiều người dùng thật thường bền hơn.

### 3.3. TVL — tiền khóa trong DeFi — `onchain/`
**TVL** (Total Value Locked) = tổng tiền người ta gửi/khóa vào một dự án DeFi.
Tiền vào nhiều = niềm tin tăng.

### 3.4. Hyperliquid — sàn on-chain — `hyperliquid/`
Một sàn phái sinh chạy on-chain → cho data funding theo GIỜ, open interest (tổng vị thế đang mở),
và dòng lệnh mua/bán THẬT (thứ sàn thường giấu).

---

## 4. Bảng tra cứu nhanh (field → nghĩa 1 dòng)

| Nhóm | Field | Nghĩa đơn giản |
|:---:|:---|:---|
| 💲 Giá | `close` · `open` · `high` · `low` | giá đóng / mở / cao nhất / thấp nhất |
| 💲 Giá | `volume` · `quote_volume` | khối lượng (số coin) / khối lượng tính ra đô |
| 💲 Giá | `returns` | % thay đổi giá |
| 💧 Thanh khoản | `amihud` | độ "khó mua bán" — mua $1 giá nhúc nhích bao nhiêu |
| 📈 Xu hướng | `dist_high` | giá cách đỉnh gần đây bao xa (gần đỉnh = khỏe) |
| 📈 Xu hướng | `kaufman_er` | xu hướng đi thẳng hay zigzag |
| ⚠️ Rủi ro | `iskew` | coin hay nhảy lên hay rớt bất ngờ |
| ⚠️ Rủi ro | `ivol` | mức dao động riêng của coin |
| ⚠️ Rủi ro | `downside_beta` | coin rớt mạnh cỡ nào khi thị trường rớt |
| ⚠️ Rủi ro | `max_ret` | cú tăng lớn nhất gần đây ("vé số") |
| ⏱️ Cao tần | `uid` | thông tin trong ngày rải đều hay dồn cục |
| ⏱️ Cao tần | `jump_ratio` | % biến động là cú nhảy đột ngột |
| 📊 Funding | `funding_carry7` · `funding_zscore21` | mức phí phái sinh / phí lệch chuẩn bao nhiêu |
| 📊 Funding | `funding_xvenue_disp7` | phí chênh lệch giữa các sàn (đám đông phân hóa) |
| ⛓️ On-chain | `active_users` | số ví hoạt động mỗi ngày (sức khỏe mạng) |
| ⛓️ On-chain | `fees_usd` · `revenue_usd` | phí & doanh thu mạng thu được |
| ⛓️ On-chain | `tvl` | tiền khóa trong DeFi |
| 🧮 Residual | `residual_returns` | biến động riêng của coin (đã bỏ phần đu theo BTC) |

---

## 5. Độ phủ — bao nhiêu coin mỗi loại data?

| Loại data | Số coin | Độ phủ (so với 200) | Ghi chú |
|:---|:---:|:---|:---|
| 💲 Giá/khối lượng *(universe chính)* | **200** | `▰▰▰▰▰▰▰▰▰▰` | nền tảng, coin chính nào cũng có |
| 📊 Funding *(phái sinh)* | **115–200** | `▰▰▰▰▰▰▰▰░░` | gần đủ |
| ⛓️ On-chain — TVL | 80 | `▰▰▰▰░░░░░░` | |
| ⛓️ On-chain — fees | 75 | `▰▰▰▰░░░░░░` | mới thêm (Artemis) |
| ⛓️ On-chain — active users (DAU) | 53 | `▰▰▰░░░░░░░` | 🆕 mới hoàn toàn |
| ⛓️ On-chain — active_addr / tx | 34 | `▰▰░░░░░░░░` | mỏng nhất ⚠️ |
| 🗂️ **Tổng coin thô** *(mọi khung)* | **619** | `▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰` | có thể mở rộng universe |

> **Vì sao on-chain mỏng?** Data on-chain free chỉ phủ những coin lớn/có blockchain riêng.
> Memecoin, token nhỏ thường không có → đây là điểm yếu cần mở rộng.

---

## 6. Tóm lại cho dễ nhớ

- **Nến giá (OHLCV)** = viên gạch nền, ai cũng có.
- **Thước đo từ giá** (thanh khoản, xu hướng, rủi ro...) = nhiều góc nhìn hơn về cùng một giá.
- **Funding + On-chain** = data KHÁC, ít người dùng → cơ hội tìm quy luật ít bị "đông người làm hỏng".
- Mỗi field là **một câu hỏi về coin**: "Có khó mua bán không? Có gần đỉnh không? Đám đông phái sinh
  đang nghĩ gì? Mạng có người dùng thật không?" — gộp nhiều câu hỏi lại để đoán coin nào sắp tăng/giảm.

---

