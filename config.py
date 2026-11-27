from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    database_path: str
    okx_rest_url: str
    okx_ws_url: str
    instrument_id: str
    rest_poll_interval: float

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token:
            raise RuntimeError(
                "Missing TELEGRAM_BOT_TOKEN. Create a bot with @BotFather "
                "and set the token in the server environment."
            )
        if not chat_id:
            raise RuntimeError(
                "Missing TELEGRAM_CHAT_ID. Set your private Telegram chat ID "
                "in the server environment."
            )

        instrument = os.getenv("OKX_INSTRUMENT_ID", "BTC-USDT").strip().upper()
        if instrument != "BTC-USDT":
            raise RuntimeError(
                "This version is intentionally configured for OKX Spot BTC-USDT only."
            )

        try:
            interval = float(os.getenv("REST_POLL_INTERVAL_SECONDS", "10"))
        except ValueError as exc:
            raise RuntimeError("REST_POLL_INTERVAL_SECONDS must be a number.") from exc
        if not 3 <= interval <= 60:
            raise RuntimeError("REST_POLL_INTERVAL_SECONDS must be between 3 and 60.")

        return cls(
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            database_path=os.getenv("DATABASE_PATH", "/data/alerts.sqlite3"),
            okx_rest_url=os.getenv("OKX_REST_URL", "https://openapi.okx.com").rstrip("/"),
            okx_ws_url=os.getenv(
                "OKX_WS_URL", "wss://ws.okx.com/ws/v5/public"
            ).strip(),
            instrument_id=instrument,
            rest_poll_interval=interval,
        )


def parse_price(raw: str) -> Decimal:
    """Parse a positive USDT price without converting through float."""
    cleaned = raw.strip().replace(",", "").replace("_", "")
    try:
        price = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Giá không hợp lệ. Ví dụ: /below 60000") from exc

    if not price.is_finite() or price <= 0:
        raise ValueError("Giá phải là một số hữu hạn lớn hơn 0.")
    if price > Decimal("1000000000"):
        raise ValueError("Giá vượt giới hạn hợp lý.")
    return price


def format_price(value: Decimal | str) -> str:
    price = Decimal(str(value))
    rendered = format(price, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    whole, dot, fraction = rendered.partition(".")
    try:
        whole = f"{int(whole):,}"
    except ValueError:
        pass
    return whole + (dot + fraction if fraction else "")
