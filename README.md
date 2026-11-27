# OKX Bitcoin 24/7 Telegram Alert Engine

Theo dõi BTC-USDT Spot trên OKX và gửi cảnh báo Telegram khi giá khớp cuối cùng (last) chạm/vượt ngưỡng bạn đặt.

## Tính năng
- OKX public WebSocket tickers là nguồn giá chính; REST ticker kiểm tra đối chiếu định kỳ.
- So sánh giá bằng Decimal, không dùng float cho quyết định ngưỡng.
- SQLite lưu ngưỡng, trạng thái đã kích hoạt và hàng đợi thông báo.
- Tự kết nối lại khi WebSocket mất kết nối.
- Không cần OKX API key vì chỉ đọc dữ liệu thị trường công khai.

> Không hệ thống nào có thể cam kết tuyệt đối 100% không mất kết nối hoặc Telegram luôn nhận tức thời. Engine có reconnect, REST fallback và hàng đợi gửi lại; thời gian nhận còn phụ thuộc mạng, OKX, Telegram và nhà cung cấp máy chủ.

## 1. Tạo Telegram Bot
1. Mở Telegram, tìm @BotFather.
2. Gửi /newbot, làm theo hướng dẫn và sao chép bot token.
3. Mở @userinfobot để xem Telegram user ID của bạn.
4. Mở bot vừa tạo và gửi /start.

Giữ bot token bí mật. Không đưa token vào GitHub hoặc tin nhắn công khai. Chỉ đặt token trong biến môi trường của máy chủ.

## 2. Chạy thử trên Windows PowerShell
Cần Python 3.11 trở lên.

```powershell
git clone https://github.com/conmeophat/Bao_gia_bitcoin_24-7_qua_telegram.git
cd Bao_gia_bitcoin_24-7_qua_telegram
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Điền TELEGRAM_BOT_TOKEN và TELEGRAM_CHAT_ID trong file .env, lưu lại rồi chạy:

```powershell
python main.py
```

Dừng chương trình bằng Ctrl+C. Máy tính phải bật và chương trình phải chạy thì cách chạy local mới theo dõi liên tục.

## 3. Cấu hình chạy 24/7 trên Railway
1. Tạo Project và service từ repository này, branch main.
2. Trong Variables, thêm TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID và DATABASE_PATH=/data/alerts.sqlite3.
3. Tạo Persistent Volume và mount vào /data.
4. Deploy service dạng worker (không cần public domain/HTTP port).
5. Xem log để xác nhận bot báo Engine started và WebSocket kết nối OKX.

Dịch vụ cloud chạy độc lập với máy tính cá nhân. Gói hosting có thể có giới hạn, chi phí hoặc chính sách sleep; hãy kiểm tra gói Railway đang dùng. Không nên coi gói miễn phí là cam kết uptime tuyệt đối.

## 4. Lệnh Telegram

| Lệnh | Ý nghĩa |
|---|---|
| /start hoặc /help | Hướng dẫn |
| /below 60000 | Báo khi giá BTC-USDT <= 60.000 USDT |
| /above 70000 | Báo khi giá BTC-USDT >= 70.000 USDT |
| /price | Giá OKX gần nhất |
| /list | Danh sách ngưỡng |
| /pause 1 | Tạm dừng cảnh báo số 1 |
| /resume 1 | Bật lại cảnh báo số 1 |
| /delete 1 | Xóa cảnh báo số 1 |
| /status | Trạng thái kết nối và giá mới nhất |

Có thể tạo nhiều ngưỡng cùng lúc. Một ngưỡng chỉ gửi một thông báo cho mỗi lần giá đi vào vùng điều kiện. Ví dụ /below 60000: khi giá xuống 60.000 hoặc thấp hơn thì báo một lần; sau đó phải tăng lên trên 60.000 rồi giảm xuống 60.000 hoặc thấp hơn mới báo lần kế tiếp.

## Nguồn giá
- Instrument: BTC-USDT, thị trường Spot.
- Field: last — giá giao dịch/khớp cuối cùng, không phải bid, ask, mark price hoặc index price.
- WebSocket: wss://ws.okx.com/ws/v5/public, channel tickers.
- REST dự phòng: https://openapi.okx.com/api/v5/market/ticker?instId=BTC-USDT.
- Thời gian trong cảnh báo hiển thị theo múi giờ Việt Nam (UTC+7).

## Kiểm thử

```powershell
python -m unittest discover -s tests -v
```

Không chia sẻ file .env, bot token hoặc file cơ sở dữ liệu có thông tin riêng tư.