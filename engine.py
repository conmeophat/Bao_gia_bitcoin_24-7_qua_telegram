from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from database import Database
from okx_client import OKXClient
from telegram_bot import TelegramBot


logger = logging.getLogger(__name__)


class PriceEngine:
    def __init__(
        self,
        database: Database,
        okx: OKXClient,
        telegram: TelegramBot,
        chat_id: str,
        rest_poll_interval: float,
    ):
        self.database = database
        self.okx = okx
        self.telegram = telegram
        self.chat_id = chat_id
        self.rest_poll_interval = rest_poll_interval
        self.ws_connected = False

    async def bootstrap(self) -> tuple[Decimal, int] | None:
        """Seed the latest known price before the long-running loops start."""
        try:
            price, timestamp_ms = await self.okx.fetch_last_price()
            self.process_price(price, timestamp_ms, "REST bootstrap")
            logger.info("Initial OKX BTC-USDT price: %s USDT", price)
            return price, timestamp_ms
        except Exception:
            logger.exception("Could not get the initial OKX ticker; background retries will continue.")
            return None

    def process_price(
        self, price: Decimal, timestamp_ms: int, source: str
    ) -> None:
        accepted = self.database.record_and_evaluate(price, timestamp_ms, source)
        if accepted:
            logger.debug(
                "BTC-USDT last=%s source=%s exchange_ts=%s",
                price,
                source,
                timestamp_ms,
            )
        else:
            logger.debug("Ignored invalid or out-of-order OKX market data.")

    async def evaluate_current_price(self) -> None:
        """Re-check a just-created/resumed rule against the latest saved ticker."""
        latest = self.database.last_price()
        if not latest:
            return
        self.process_price(
            Decimal(latest["price"]),
            int(latest["ts_ms"]),
            "Telegram command check",
        )

    async def websocket_loop(self) -> None:
        reconnect_delay = 1.0
        while True:
            try:
                logger.info("Connecting to OKX public WebSocket...")
                async for price, timestamp_ms in self.okx.stream_prices():
                    if not self.ws_connected:
                        logger.info("OKX WebSocket connected and receiving ticker updates.")
                    self.ws_connected = True
                    reconnect_delay = 1.0
                    self.process_price(price, timestamp_ms, "OKX WebSocket")
                raise ConnectionError("OKX WebSocket stream ended.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ws_connected = False
                logger.warning(
                    "OKX WebSocket disconnected: %s. Reconnect in %.1f seconds.",
                    exc,
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

    async def rest_fallback_loop(self) -> None:
        """Periodic REST reconciliation protects against a stale/broken WS connection."""
        while True:
            try:
                await asyncio.sleep(self.rest_poll_interval)
                price, timestamp_ms = await self.okx.fetch_last_price()
                self.process_price(price, timestamp_ms, "OKX REST fallback")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OKX REST fallback check failed; will retry.")

    async def notification_outbox_loop(self) -> None:
        """Retry unsent alert messages until Telegram confirms successful delivery."""
        while True:
            pending = self.database.pending_notifications(limit=20)
            if not pending:
                await asyncio.sleep(1)
                continue

            for notification in pending:
                try:
                    await self.telegram.send_message(
                        self.chat_id, notification["message"]
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.database.mark_notification_failed(
                        int(notification["id"]), str(exc)
                    )
                    attempts = int(notification.get("attempts", 0)) + 1
                    delay = min(2 ** min(attempts, 6), 60)
                    logger.exception(
                        "Telegram delivery failed for outbox item %s; retry in %ss.",
                        notification["id"],
                        delay,
                    )
                    await asyncio.sleep(delay)
                    break
                else:
                    self.database.mark_notification_sent(int(notification["id"]))
                    logger.info(
                        "Telegram alert delivered: outbox=%s alert=%s",
                        notification["id"],
                        notification["alert_id"],
                    )
                    await asyncio.sleep(1)

    def status_snapshot(self) -> dict:
        latest = self.database.last_price()
        age_seconds = None
        if latest:
            age_seconds = max(0.0, time.time() - int(latest["ts_ms"]) / 1000)
        return {
            "ws_connected": self.ws_connected,
            "last_price": latest,
            "age_seconds": age_seconds,
            "active_alerts": self.database.count_active_alerts(),
        }
