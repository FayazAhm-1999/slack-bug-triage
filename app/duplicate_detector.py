"""Duplicate detection via Claude semantic comparison.

TF-IDF was replaced because it fails on paraphrases: "bot should react after
parsing a ticket" and "bot should acknowledge ticket creation with a reaction"
share almost no non-stopword terms after IDF weighting but describe the same
problem. Claude understands semantic equivalence, synonyms, and implied intent —
exactly the cases where lexical similarity breaks down.

Trade-off: one Claude call per new report vs. zero API calls for TF-IDF.
At demo scale (infrequent writes, 50-issue corpus) the cost is negligible.

Upgrade path for high volume: pre-compute embeddings (sentence-transformers
all-MiniLM-L6-v2) and store in a vector DB (pgvector, Chroma). The
DuplicateResult interface is unchanged — swap only this module.
"""

import json
import logging

import anthropic

from app import github_client
from app.config import settings
from app.models import BugReport, DuplicateResult

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

_SYSTEM_PROMPT = """\
You are a duplicate bug report detector. Given a new bug report and a list of \
existing GitHub issues, determine whether the new report describes the same \
underlying problem as any existing issue.

Focus on semantic meaning, not literal word matching. Two reports are duplicates \
if they describe the same root cause or expected behavior — even if worded differently.

Return ONLY a valid JSON object:
{
  "is_duplicate": true | false,
  "issue_number": <integer> | null,
  "similarity_score": <float 0.0–1.0>
}

Rules:
- Set is_duplicate to true only when confident the reports describe the same problem.
- issue_number must be the GitHub issue number of the match, or null if none.
- similarity_score reflects semantic closeness (1.0 = same problem, 0.0 = unrelated).
- Output raw JSON only — no markdown, no explanation.
"""


def _format_issue_list(issues: list[dict]) -> str:
    lines = []
    for issue in issues:
        number = issue.get("number", "?")
        title = issue.get("title", "")
        body_snippet = (issue.get("body") or "")[:150].replace("\n", " ")
        lines.append(f"#{number}: {title} — {body_snippet}")
    return "\n".join(lines)


async def check_duplicate(bug: BugReport) -> DuplicateResult:
    """Return a DuplicateResult indicating whether the bug matches an existing issue.

    Falls back to is_duplicate=False on any error so the caller always
    proceeds to create an issue rather than silently dropping a report.
    """
    try:
        issues = await github_client.get_open_issues(count=50)
    except Exception:
        logger.exception("Failed to fetch GitHub issues for duplicate check")
        return DuplicateResult(is_duplicate=False, similarity_score=0.0)

    if not issues:
        return DuplicateResult(is_duplicate=False, similarity_score=0.0)

    issue_index = {issue["number"]: issue for issue in issues}
    user_content = (
        f"New bug report:\n"
        f"Title: {bug.title}\n"
        f"Summary: {bug.summary}\n"
        f"Component: {bug.affected_component}\n\n"
        f"Existing issues:\n{_format_issue_list(issues)}"
    )

    for attempt in range(2):
        if attempt == 1:
            user_content += (
                "\n\nIMPORTANT: Your previous response was not valid JSON. "
                "Respond with the raw JSON object only."
            )

        try:
            response = await _client.messages.create(
                model=_MODEL,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            data = json.loads(raw)
            is_dup = bool(data.get("is_duplicate", False))
            issue_number = data.get("issue_number")
            score = float(data.get("similarity_score", 0.0))

            if is_dup and issue_number and issue_number in issue_index:
                matched = issue_index[issue_number]
                return DuplicateResult(
                    is_duplicate=True,
                    similarity_score=score,
                    existing_issue_number=issue_number,
                    existing_issue_url=matched.get("html_url"),
                )

            return DuplicateResult(is_duplicate=False, similarity_score=score)

        except json.JSONDecodeError as exc:
            logger.warning(
                "Claude returned invalid JSON for duplicate check (attempt %d): %s",
                attempt + 1, exc,
            )
        except anthropic.APIError as exc:
            logger.error("Anthropic API error during duplicate check: %s", exc)
            return DuplicateResult(is_duplicate=False, similarity_score=0.0)

    logger.error("Duplicate detection failed after 2 attempts")
    return DuplicateResult(is_duplicate=False, similarity_score=0.0)
