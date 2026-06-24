"""Async GitHub REST API wrapper.

All outbound GitHub calls go through this module.
Requires a personal access token with repo scope.

A single shared AsyncClient is reused across all calls for connection
pooling and keep-alive.  Call close() during app shutdown.
"""

import logging
import httpx

from app.config import settings
from app.models import BugReport

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.github.com"

_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))


async def close() -> None:
    """Close the shared HTTP client. Call during app shutdown."""
    await _client.aclose()


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"token {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo_url(path: str) -> str:
    return f"{_BASE_URL}/repos/{settings.github_owner}/{settings.github_repo_name}{path}"


async def get_open_issues(count: int = 50) -> list[dict]:
    """Fetch the most recent open issues for duplicate detection."""
    resp = await _client.get(
        _repo_url("/issues"),
        headers=_headers(),
        params={"state": "open", "per_page": count},
    )
    if resp.status_code != 200:
        logger.error(
            "get_open_issues HTTP %s: %s",
            resp.status_code,
            resp.json().get("message", "<no message>"),
        )
        return []
    return resp.json()


async def create_issue(title: str, body: str, labels: list[str]) -> dict:
    """Create a GitHub issue and return the response dict (includes html_url, number)."""
    resp = await _client.post(
        _repo_url("/issues"),
        headers=_headers(),
        json={"title": title, "body": body, "labels": labels},
    )
    if resp.status_code not in (200, 201):
        logger.error(
            "create_issue HTTP %s: %s",
            resp.status_code,
            resp.json().get("message", "<no message>"),
        )
        resp.raise_for_status()
    return resp.json()


async def add_comment(issue_number: int, body: str) -> None:
    """Append a comment to an existing GitHub issue."""
    resp = await _client.post(
        _repo_url(f"/issues/{issue_number}/comments"),
        headers=_headers(),
        json={"body": body},
    )
    if resp.status_code not in (200, 201):
        logger.error(
            "add_comment HTTP %s: %s",
            resp.status_code,
            resp.json().get("message", "<no message>"),
        )


def format_issue_body(bug: BugReport, slack_thread_url: str | None) -> str:
    """Render the GitHub issue markdown body. Pure function."""
    steps = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(bug.reproduction_steps))
    source = f"[View Slack thread]({slack_thread_url})" if slack_thread_url else "_Not available_"

    return f"""\
## Summary

{bug.summary}

## Severity

{bug.severity.capitalize()}

## Affected Component

{bug.affected_component or "_Not specified_"}

## Reproduction Steps

{steps or "_Not provided_"}

## Expected Behavior

{bug.expected_behavior or "_Not specified_"}

## Actual Behavior

{bug.actual_behavior or "_Not specified_"}

## Source Slack Thread

{source}
"""


def build_labels(severity: str) -> list[str]:
    return ["bug", f"severity:{severity}"]
