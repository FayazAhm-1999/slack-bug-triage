"""Shared Pydantic models — data contracts only, no business logic."""

from typing import Literal
from pydantic import BaseModel


class BugReport(BaseModel):
    title: str
    summary: str
    severity: Literal["low", "medium", "high", "critical"]
    affected_component: str
    reproduction_steps: list[str]
    expected_behavior: str
    actual_behavior: str


class QualityResult(BaseModel):
    quality_score: int
    passed: bool
    missing_fields: list[str]


class DuplicateResult(BaseModel):
    is_duplicate: bool
    similarity_score: float
    existing_issue_number: int | None = None
    existing_issue_url: str | None = None


# ---------------------------------------------------------------------------
# Slack event envelope models
# ---------------------------------------------------------------------------

class SlackEventPayload(BaseModel):
    """Outer Slack Events API envelope."""
    token: str | None = None
    team_id: str | None = None
    type: str                   # "url_verification" | "event_callback"
    challenge: str | None = None
    event: dict | None = None


class MessageEvent(BaseModel):
    type: str
    channel: str
    user: str = ""
    text: str = ""
    ts: str
    thread_ts: str | None = None
    subtype: str | None = None  # "bot_message" | "message_changed" | etc.


class ReactionItem(BaseModel):
    type: str       # "message" | "file" | "file_comment"
    channel: str
    ts: str         # timestamp of the message that received the reaction


class ReactionEvent(BaseModel):
    type: str
    user: str       # who added the reaction
    reaction: str   # "bug" (without colons)
    item: ReactionItem


class SlackThreadContext(BaseModel):
    """Assembled Slack thread context passed downstream to Claude."""
    channel_id: str
    message_ts: str
    thread_ts: str
    original_message: str
    thread_replies: list[str]
    permalink: str | None = None
