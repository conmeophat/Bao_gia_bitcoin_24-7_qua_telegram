from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import AsyncIterator

import aiohttp


logger = logging.getLogger(__name__)


class OKXClient:
    """Read-only client for the OKX BTC-USDT Spot public market feeds."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        rest_url: str,
        ws_url: str,
        instrument_id: str,
    ):
        self.session = session
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url
        self.instrument_id = instrument_id

    async def fetch_last_price(self) -> tuple[Decimal, int]:
        url = f"{self.rest_url}/api/v5/market/ticker"
        async with self.session.get(
            url, params={"instId": self.instrument_id}, timeout=15
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)

        if payload.get("code") != "0":
            raise RuntimeError(
                f"OKX REST error {payload.get('code')}: {payload.get('msg')}"
            )
        data = payload.get("data") or []
        if not data:
            raise RuntimeError("OKX REST returned no ticker data.")

        item = data[0]
        if item.get("instId") != self.instrument_id:
            raise RuntimeError("OKX returned a different instrument than requested.")

        try:
            price = Decimal(str(item["last"]))
            timestamp_ms = int(item["ts"])
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            raise RuntimeError("Invalid ticker payload received from OKX REST.") from exc

        if not price.is_finite() or price <= 0 or timestamp_ms <= 0:
            raise RuntimeError("OKX returned a non-positive price or invalid timestamp.")
        return price, timestamp_ms

    async def stream_prices(self) -> AsyncIterator[tuple[Decimal, int]]:
        """Yield last traded prices from the public tickers WebSocket channel."""
        async with self.session.ws_connect(
            self.ws_url,
            heartbeat=20,
            receive_timeout=45,
            timeout=20,
            max_msg_size=1024 * 1024,
        ) as websocket:
            await websocket.send_json(
                {
                    "op": "subscribe",
                    "args": [
                        {
                            "channel": "tickers",
                            "instId": self.instrument_id,
                        }
                    ],
                }
            )
            logger.info(
                "Subscribed to OKX public tickers for %s", self.instrument_id
            )

            while True:
                try:
                    message = await websocket.receive(timeout=25)
                except asyncio.TimeoutError:
                    # OKX application-level heartbeat: send text ping after an idle interval.
                    await websocket.send_str("ping")
                    continue

                if message.type == aiohttp.WSMsgType.TEXT:
                    if message.data == "pong":
                        continue

                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        logger.warning("Ignoring non-JSON OKX WebSocket message.")
                        continue

                    if payload.get("event") == "error":
                        raise RuntimeError(
                            f"OKX WebSocket error {payload.get('code')}: "
                            f"{payload.get('msg')}"
                        )
                    if payload.get("event"):
                        logger.debug("OKX WebSocket event: %s", payload.get("event"))
                        continue

                    for item in payload.get("data") or []:
                        if item.get("instId") != self.instrument_id:
                            continue
                        try:
                            price = Decimal(str(item["last"]))
                            timestamp_ms = int(item["ts"])
                        except (
                            KeyError,
                            ValueError,
                            TypeError,
                            InvalidOperation,
                        ):
                            logger.warning("Ignoring malformed OKX ticker message.")
                            continue

                        if price.is_finite() and price > 0 and timestamp_ms > 0:
                            yield price, timestamp_ms

                elif message.type in {
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                }:
                    raise ConnectionError("OKX WebSocket closed or returned an error.")
                else:
                    # Ignore control frames and other non-text messages.
                    continue
