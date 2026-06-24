"""Anthropic Claude API wrapper for structured bug extraction.

Uses a strict system prompt that instructs Claude to return raw JSON only.
Retries once on parse failure before giving up — avoids infinite loops while
tolerating the occasional malformed response.

A single shared AsyncAnthropic client is reused across calls.
"""

import json
import logging

import anthropic
from pydantic import ValidationError

from app.config import settings
from app.models import BugReport

logger = logging.getLogger(__name__)

# claude-haiku-4-5-20251001 balances quality and speed well for structured extraction.
# Swap to claude-sonnet-4-6 for higher quality at greater cost.
_MODEL = "claude-haiku-4-5-20251001"

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

_SYSTEM_PROMPT = """\
You are a bug report extraction assistant. Given a Slack conversation, extract
structured information and return ONLY a valid JSON object with exactly these fields:

{
  "title": "concise issue title",
  "summary": "one-paragraph description of the bug",
  "severity": "low" | "medium" | "high" | "critical" | "unknown",
  "affected_component": "the system/feature/page affected",
  "reproduction_steps": ["step 1", "step 2", ...],
  "expected_behavior": "what should happen",
  "actual_behavior": "what actually happens"
}

Rules:
- Output raw JSON only — no markdown fences, no explanation, no preamble.
- If a text field cannot be determined from the conversation, use an empty string; if a list field cannot be determined, use an empty array.
- severity must be exactly one of: low, medium, high, critical, unknown — never empty. Use "unknown" if it cannot be determined from the conversation.
"""


async def extract_bug_report(
    original_message: str,
    thread_replies: list[str],
) -> BugReport | None:
    """Extract a structured BugReport from a Slack thread.

    Returns None if Claude fails to produce valid JSON after one retry.
    """
    replies_text = "\n".join(f"- {r}" for r in thread_replies) if thread_replies else "(no replies)"
    user_content = f"Original message:\n{original_message}\n\nThread replies:\n{replies_text}"

    for attempt in range(2):
        if attempt == 1:
            # Give Claude a nudge on the retry
            user_content += "\n\nIMPORTANT: Your previous response was not valid JSON. Respond with the raw JSON object only."

        try:
            response = await _client.messages.create(
                model=_MODEL,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = response.content[0].text.strip()

            # Strip accidental markdown fences if Claude ignores instructions
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            data = json.loads(raw)
            return BugReport.model_validate(data)

        except json.JSONDecodeError as exc:
            logger.warning("Claude returned invalid JSON (attempt %d): %s", attempt + 1, exc)
        except ValidationError as exc:
            logger.warning("Claude JSON failed BugReport validation (attempt %d): %s", attempt + 1, exc)
        except anthropic.APIError as exc:
            logger.error("Anthropic API error: %s", exc)
            return None

    logger.error("Claude extraction failed after 2 attempts")
    return None
