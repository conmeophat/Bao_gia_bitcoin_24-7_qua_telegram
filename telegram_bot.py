from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import aiohttp

from config import format_price, parse_price
from database import Database
if TYPE_CHECKING:
    from engine import PriceEngine


logger = logging.getLogger(__name__)
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_chat_id: str,
        session: aiohttp.ClientSession,
        database: Database,
        engine: PriceEngine,
    ):
        self.token = token
        self.allowed_chat_id = str(allowed_chat_id)
        self.session = session
        self.database = database
        self.engine = engine
        self.api_base = f"https://api.telegram.org/bot{token}"

    async def api(self, method: str, payload: dict | None = None):
        timeout_seconds = 40 if method == "getUpdates" else 20
        async with self.session.post(
            f"{self.api_base}/{method}",
            json=payload or {},
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            try:
                result = await response.json(content_type=None)
            except Exception as exc:
                body = await response.text()
                raise RuntimeError(
                    f"Telegram {method} returned invalid JSON (HTTP {response.status})."
                ) from exc

            if response.status != 200 or not result.get("ok"):
                description = result.get("description", "Unknown Telegram API error")
                error_code = result.get("error_code", response.status)
                parameters = result.get("parameters") or {}
                retry_after = parameters.get("retry_after")
                extra = f"; retry_after={retry_after}" if retry_after else ""
                raise RuntimeError(
                    f"Telegram {method} failed ({error_code}): {description}{extra}"
                )
            return result.get("result")

    async def verify(self) -> str:
        identity = await self.api("getMe")
        username = identity.get("username", "unknown")
        logger.info("Telegram bot authenticated as @%s", username)
        return username

    async def send_message(self, chat_id: str, text: str) -> None:
        await self.api(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": text,
                "disable_web_page_preview": True,
            },
        )

    @staticmethod
    def help_text() -> str:
        return (
            "🤖 OKX BITCOIN ALERT ENGINE\n\n"
            "Đặt ngưỡng giá trực tiếp bằng các lệnh:\n"
            "/below 60000  — báo khi BTC ≤ 60.000 USDT\n"
            "/above 70000  — báo khi BTC ≥ 70.000 USDT\n\n"
            "Các lệnh quản lý:\n"
            "/price        — giá OKX gần nhất\n"
            "/list         — danh sách ngưỡng\n"
            "/pause 1      — tạm dừng ngưỡng số 1\n"
            "/resume 1     — bật lại ngưỡng số 1\n"
            "/delete 1     — xóa ngưỡng số 1\n"
            "/status       — tình trạng kết nối engine\n"
            "/help         — hướng dẫn này\n\n"
            "Giá sử dụng: OKX Spot BTC-USDT, trường last.\n"
            "Một ngưỡng chỉ báo một lần khi đi vào vùng điều kiện; "
            "giá phải quay ra ngoài vùng rồi đi vào lại thì mới báo lần tiếp theo."
        )

    @staticmethod
    def _parse_id(parts: list[str]) -> int:
        if len(parts) != 2:
            raise ValueError("Cú pháp: /pause 1 (thay 1 bằng mã cảnh báo).")
        try:
            alert_id = int(parts[1])
        except ValueError as exc:
            raise ValueError("Mã cảnh báo phải là số nguyên.") from exc
        if alert_id <= 0:
            raise ValueError("Mã cảnh báo không hợp lệ.")
        return alert_id

    def _price_description(self) -> str:
        status = self.engine.status_snapshot()
        latest = status["last_price"]
        if not latest:
            return "Chưa nhận được dữ liệu giá từ OKX."

        price = format_price(Decimal(latest["price"]))
        exchange_time = datetime.fromtimestamp(
            int(latest["ts_ms"]) / 1000, tz=VN_TZ
        ).strftime("%d/%m/%Y %H:%M:%S.%f")[:-3]
        age = status["age_seconds"]
        age_text = "không rõ" if age is None else f"{age:.1f} giây trước"
        return (
            f"BTC-USDT Spot: {price} USDT\n"
            f"Nguồn: OKX {latest['source']}\n"
            f"Thời điểm dữ liệu sàn: {exchange_time} (UTC+7)\n"
            f"Độ mới dữ liệu: {age_text}"
        )

    async def _handle_text(self, text: str) -> None:
        parts = text.strip().split()
        if not parts:
            return

        command = parts[0].split("@", 1)[0].lower()

        if command in {"/start", "/help"}:
            await self.send_message(self.allowed_chat_id, self.help_text())
            return

        if command in {"/below", "/above"}:
            if len(parts) != 2:
                await self.send_message(
                    self.allowed_chat_id,
                    f"Cú pháp: {command} 60000\nVí dụ: {command} 60000",
                )
                return
            if self.database.count_active_alerts() >= 100:
                await self.send_message(
                    self.allowed_chat_id,
                    "Bạn đã đạt giới hạn 100 cảnh báo đang bật. Hãy tạm dừng hoặc xóa bớt.",
                )
                return

            try:
                threshold = parse_price(parts[1])
            except ValueError as exc:
                await self.send_message(self.allowed_chat_id, str(exc))
                return

            direction = "below" if command == "/below" else "above"
            alert = self.database.create_alert(direction, threshold)
            await self.engine.evaluate_current_price()
            comparator = "≤" if direction == "below" else "≥"
            await self.send_message(
                self.allowed_chat_id,
                f"✅ Đã tạo cảnh báo #{alert['id']}\n"
                f"BTC-USDT {comparator} {format_price(threshold)} USDT\n"
                "Hệ thống sẽ kiểm tra giá OKX liên tục.",
            )
            return

        if command == "/price":
            await self.send_message(
                self.allowed_chat_id, self._price_description()
            )
            return

        if command == "/list":
            alerts = self.database.list_alerts()
            if not alerts:
                await self.send_message(
                    self.allowed_chat_id,
                    "Bạn chưa tạo ngưỡng nào. Ví dụ: /below 60000",
                )
                return

            lines = ["📋 DANH SÁCH CẢNH BÁO"]
            for alert in alerts:
                comparator = "≤" if alert["direction"] == "below" else "≥"
                state = "ĐANG BẬT" if alert["active"] else "ĐÃ TẠM DỪNG"
                trigger_state = (
                    "đang trong vùng đã báo"
                    if alert["triggered"]
                    else "chờ điều kiện"
                )
                lines.append(
                    f"#{alert['id']}  BTC {comparator} "
                    f"{format_price(Decimal(alert['threshold']))} USDT\n"
                    f"   {state} · {trigger_state} · "
                    f"đã báo {alert['trigger_count']} lần"
                )
            await self.send_message(self.allowed_chat_id, "\n".join(lines))
            return

        if command in {"/pause", "/resume", "/delete"}:
            try:
                alert_id = self._parse_id(parts)
            except ValueError as exc:
                await self.send_message(self.allowed_chat_id, str(exc))
                return

            if command == "/delete":
                changed = self.database.delete_alert(alert_id)
                message = (
                    f"🗑 Đã xóa cảnh báo #{alert_id}."
                    if changed
                    else f"Không tìm thấy cảnh báo #{alert_id}."
                )
            else:
                active = command == "/resume"
                changed = self.database.set_alert_active(alert_id, active)
                if changed:
                    if active:
                        await self.engine.evaluate_current_price()
                    message = (
                        f"✅ Đã bật lại cảnh báo #{alert_id}."
                        if active
                        else f"⏸ Đã tạm dừng cảnh báo #{alert_id}."
                    )
                else:
                    message = f"Không tìm thấy cảnh báo #{alert_id}."

            await self.send_message(self.allowed_chat_id, message)
            return

        if command == "/status":
            status = self.engine.status_snapshot()
            ws_label = "ĐÃ KẾT NỐI" if status["ws_connected"] else "ĐANG KẾT NỐI LẠI"
            latest = status["last_price"]
            if latest and status["age_seconds"] is not None:
                age = status["age_seconds"]
                freshness = (
                    "MỚI"
                    if age <= 30
                    else "CŨ - cần kiểm tra kết nối"
                )
            else:
                freshness = "CHƯA CÓ DỮ LIỆU"
            message = (
                "🩺 TRẠNG THÁI ENGINE\n"
                f"OKX WebSocket: {ws_label}\n"
                f"REST đối chiếu: mỗi {self.engine.rest_poll_interval:g} giây\n"
                f"Dữ liệu giá: {freshness}\n"
                f"Cảnh báo đang bật: {status['active_alerts']}\n\n"
                f"{self._price_description()}"
            )
            await self.send_message(self.allowed_chat_id, message)
            return

        await self.send_message(
            self.allowed_chat_id,
            "Không nhận diện được lệnh. Gửi /help để xem hướng dẫn.",
        )

    async def handle_update(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        # Only accept commands from the configured private chat, never public groups.
        if chat.get("type") != "private" or chat_id != self.allowed_chat_id:
            logger.warning("Ignored Telegram update from an unapproved chat.")
            return

        text = message.get("text")
        if not text:
            return

        try:
            await self._handle_text(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not process Telegram command.")
            try:
                await self.send_message(
                    self.allowed_chat_id,
                    "Đã xảy ra lỗi khi xử lý lệnh. Hãy thử lại hoặc gửi /status.",
                )
            except Exception:
                logger.exception("Could not send Telegram command error response.")

    async def polling_loop(self, database: Database) -> None:
        offset = int(database.get_setting("telegram_update_offset", "0") or "0")
        while True:
            try:
                updates = await self.api(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 25,
                        "allowed_updates": ["message"],
                    },
                )
                for update in updates or []:
                    await self.handle_update(update)
                    offset = max(offset, int(update["update_id"]) + 1)
                    database.set_setting("telegram_update_offset", str(offset))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram getUpdates failed; retrying shortly.")
                await asyncio.sleep(3)
