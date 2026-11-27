from __future__ import annotations

import asyncio
import logging
import sys

import aiohttp

from config import Settings
from database import Database
from engine import PriceEngine
from okx_client import OKXClient
from telegram_bot import TelegramBot


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def run() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()

    timeout = aiohttp.ClientTimeout(total=45, connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        okx = OKXClient(
            session=session,
            rest_url=settings.okx_rest_url,
            ws_url=settings.okx_ws_url,
            instrument_id=settings.instrument_id,
        )

        # The bot is intentionally restricted to one configured private Telegram chat.
        # The engine and bot reference each other, so wire them after construction.
        engine_placeholder = type("EngineReference", (), {})()
        telegram = TelegramBot(
            token=settings.telegram_bot_token,
            allowed_chat_id=settings.telegram_chat_id,
            session=session,
            database=database,
            engine=engine_placeholder,
        )
        engine = PriceEngine(
            database=database,
            okx=okx,
            telegram=telegram,
            chat_id=settings.telegram_chat_id,
            rest_poll_interval=settings.rest_poll_interval,
        )
        telegram.engine = engine

        username = await telegram.verify()
        initial = await engine.bootstrap()
        if initial:
            startup_price = f"{initial[0]} USDT"
        else:
            startup_price = "đang chờ dữ liệu giá OKX đầu tiên"

        await telegram.send_message(
            settings.telegram_chat_id,
            "✅ OKX BITCOIN ALERT ENGINE ĐÃ KHỞI ĐỘNG\n"
            f"Bot: @{username}\n"
            "Thị trường: OKX Spot BTC-USDT\n"
            f"Giá ban đầu: {startup_price}\n"
            "Nguồn chính: WebSocket tickers\n"
            f"REST đối chiếu: mỗi {settings.rest_poll_interval:g} giây\n"
            "Gửi /help để quản lý ngưỡng cảnh báo.",
        )
        logging.getLogger(__name__).info(
            "Engine started for %s; database=%s",
            settings.instrument_id,
            settings.database_path,
        )

        await asyncio.gather(
            engine.websocket_loop(),
            engine.rest_fallback_loop(),
            engine.notification_outbox_loop(),
            telegram.polling_loop(database),
        )


def main() -> None:
    configure_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Engine stopped by operator.")
    except Exception:
        logging.getLogger(__name__).exception("Engine stopped due to a fatal error.")
        raise


if __name__ == "__main__":
    main()
