"""Async Slack API wrapper.

All outbound Slack calls go through this module. Slack always returns
HTTP 200 — actual errors are signalled via response.json()["ok"] == False.
"""

import logging
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://slack.com/api"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.slack_bot_token}"}


async def get_thread_messages(channel_id: str, thread_ts: str) -> list[dict]:
    """Fetch all messages in a thread (original + replies), filtered to human messages."""
    messages: list[dict] = []
    cursor: str | None = None

    async with httpx.AsyncClient() as client:
        while True:
            params: dict = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor

            resp = await client.get(
                f"{_BASE_URL}/conversations.replies",
                headers=_headers(),
                params=params,
            )
            data = resp.json()

            if not data.get("ok"):
                logger.error("conversations.replies error: %s", data.get("error"))
                return messages

            for msg in data.get("messages", []):
                # Skip bot messages so Claude only sees human content
                if msg.get("subtype") not in ("bot_message", "message_changed", "message_deleted"):
                    messages.append(msg)

            meta = data.get("response_metadata", {})
            cursor = meta.get("next_cursor")
            if not cursor:
                break

    return messages


async def get_message_permalink(channel_id: str, message_ts: str) -> str | None:
    """Return a permanent link to a Slack message, or None on failure."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_BASE_URL}/chat.getPermalink",
            headers=_headers(),
            params={"channel": channel_id, "message_ts": message_ts},
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error("chat.getPermalink error: %s", data.get("error"))
            return None
        return data.get("permalink")


async def post_message(channel_id: str, thread_ts: str, text: str) -> None:
    """Post a reply into a Slack thread."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_BASE_URL}/chat.postMessage",
            headers={**_headers(), "Content-Type": "application/json"},
            json={
                "channel": channel_id,
                "thread_ts": thread_ts,
                "text": text,
            },
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error("chat.postMessage error: %s", data.get("error"))
