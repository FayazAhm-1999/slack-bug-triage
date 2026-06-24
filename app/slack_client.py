"""Async Slack API wrapper.

All outbound Slack calls go through this module. Slack always returns
HTTP 200 — actual errors are signalled via response.json()["ok"] == False.

A single shared AsyncClient is reused across all calls for connection
pooling and keep-alive.  Call close() during app shutdown.
"""

import logging
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://slack.com/api"

_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))


async def close() -> None:
    """Close the shared HTTP client. Call during app shutdown."""
    await _client.aclose()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.slack_bot_token}"}


async def get_thread_messages(channel_id: str, thread_ts: str) -> list[dict]:
    """Fetch all messages in a thread (original + replies), filtered to human messages."""
    messages: list[dict] = []
    cursor: str | None = None
    max_pages = 10  # guard against pathologically large threads

    for _ in range(max_pages):
        params: dict = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor

        resp = await _client.get(
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
    resp = await _client.get(
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
    resp = await _client.post(
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


async def add_reaction(channel_id: str, message_ts: str, reaction: str) -> None:
    """Add an emoji reaction to a Slack message.

    Silently ignores already_reacted — safe to call even if the bot reacted before.
    Requires the reactions:write OAuth scope.
    """
    resp = await _client.post(
        f"{_BASE_URL}/reactions.add",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"channel": channel_id, "timestamp": message_ts, "name": reaction},
    )
    data = resp.json()
    error = data.get("error")
    if error and error != "already_reacted":
        logger.error("reactions.add error: %s", error)
