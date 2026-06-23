"""Thin orchestration adapter between Slack thread context and Claude extraction.

Keeping this separate from claude_client means the prompt-assembly logic
is independently testable without hitting the Anthropic API.
"""

import logging

from app import claude_client
from app.models import BugReport, SlackThreadContext

logger = logging.getLogger(__name__)


async def extract_from_thread(context: SlackThreadContext) -> BugReport | None:
    """Assemble thread text and delegate to Claude for extraction."""
    if not context.original_message.strip():
        logger.warning("Empty message in thread %s — skipping extraction", context.thread_ts)
        return None

    return await claude_client.extract_bug_report(
        original_message=context.original_message,
        thread_replies=context.thread_replies,
    )
