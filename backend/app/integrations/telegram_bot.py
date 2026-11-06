from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from ..config import settings

logger = logging.getLogger("jarvis.telegram")
# Telegram's Bot API embeds the token in the request path. HTTPX logs full
# request URLs at INFO, so suppress those access logs to keep the token secret.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
Handler = Callable[[dict[str, Any]], Awaitable[None]]


class TelegramBot:
    def __init__(self, token: str, allowed_user_id: str):
        self.token = token
        self.allowed_user_id = str(allowed_user_id).strip()
        self.base_url = f"https://api.telegram.org/bot{token}"

    async def _call(self, client: httpx.AsyncClient, method: str, payload: dict[str, Any]):
        response = await client.post(f"{self.base_url}/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram Bot API returned an unsuccessful response")
        return data.get("result")

    async def send_message(self, client: httpx.AsyncClient, text: str) -> None:
        # Telegram limits text messages to 4096 characters; leave room for safety.
        chunks = [text[i:i + 4000] for i in range(0, max(1, len(text)), 4000)]
        for chunk in chunks:
            await self._call(client, "sendMessage", {"chat_id": self.allowed_user_id, "text": chunk})

    async def run(self, handler: Handler) -> None:
        offset: int | None = None
        timeout = httpx.Timeout(35.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            while True:
                try:
                    payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message"]}
                    if offset is not None:
                        payload["offset"] = offset
                    updates = await self._call(client, "getUpdates", payload)
                    for update in updates or []:
                        offset = int(update["update_id"]) + 1
                        message = update.get("message", {})
                        sender_id = str((message.get("from") or {}).get("id", ""))
                        # Ignore unauthorized messages without replying or logging their contents.
                        if sender_id != self.allowed_user_id:
                            continue
                        message_text = message.get("text", "").strip()
                        if message_text:
                            await handler({"text": message_text, "chat_id": str((message.get("chat") or {}).get("id", ""))})
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, ValueError, RuntimeError, KeyError) as exc:
                    # Do not log the Bot API URL because it contains the bot token.
                    logger.warning("Telegram polling failed (%s); retrying in 5 seconds", type(exc).__name__)
                    await asyncio.sleep(5)


def configured_bot() -> TelegramBot | None:
    if not settings.telegram_bot_token or not settings.telegram_user_id:
        return None
    return TelegramBot(settings.telegram_bot_token, settings.telegram_user_id)


async def send_private_telegram_message(text: str) -> bool:
    bot = configured_bot()
    if bot is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            await bot.send_message(client, text)
        return True
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        logger.warning("Telegram reminder delivery failed (%s)", type(exc).__name__)
        return False
