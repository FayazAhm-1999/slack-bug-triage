"""Integration tests for the two triage pipelines and authorization gate.

All external I/O is mocked; these tests exercise the wiring between modules
without making real API calls.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.main import _process_thread, handle_reaction_event
from app.models import BugReport, DuplicateResult, ReactionEvent, ReactionItem


@pytest.fixture()
def good_bug() -> BugReport:
    return BugReport(
        title="Login crashes on Safari",
        summary="Users on Safari 17 cannot log in — the page throws a JS error.",
        severity="high",
        affected_component="auth/login",
        reproduction_steps=["Open Safari 17", "Navigate to /login", "Click Sign In"],
        expected_behavior="User is redirected to dashboard",
        actual_behavior="Page throws TypeError and goes blank",
    )


@pytest.fixture()
def low_quality_bug(good_bug) -> BugReport:
    """A bug report missing enough fields to score below the 80-point threshold."""
    return good_bug.model_copy(update={"title": "", "summary": ""})


# ---------------------------------------------------------------------------
# Path 1 — bug channel message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path1_creates_github_issue(good_bug):
    """Message in bug channel → new GitHub issue created, confirmation posted to Slack."""
    with (
        patch("app.main.slack_client.add_reaction", new_callable=AsyncMock),
        patch("app.main.slack_client.get_thread_messages", new_callable=AsyncMock,
              return_value=[{"text": "login is broken", "subtype": None}]),
        patch("app.main.slack_client.get_message_permalink", new_callable=AsyncMock,
              return_value="https://slack.com/link"),
        patch("app.main.slack_client.post_message", new_callable=AsyncMock) as mock_post,
        patch("app.main.bug_extractor.extract_from_thread", new_callable=AsyncMock,
              return_value=good_bug),
        patch("app.main.duplicate_detector.check_duplicate", new_callable=AsyncMock,
              return_value=DuplicateResult(is_duplicate=False, similarity_score=0.1)),
        patch("app.main.github_client.create_issue", new_callable=AsyncMock,
              return_value={"number": 42, "html_url": "https://github.com/org/repo/issues/42"}),
        patch("app.main.github_client.format_issue_body", return_value="body"),
        patch("app.main.github_client.build_labels", return_value=["bug", "severity:high"]),
    ):
        await _process_thread("C123456", "1000000000.000001", run_quality_gate=False)

    mock_post.assert_called_once()
    confirmation = mock_post.call_args[0][2]
    assert "https://github.com/org/repo/issues/42" in confirmation


@pytest.mark.asyncio
async def test_path1_skips_quality_gate(low_quality_bug):
    """Path 1 creates an issue even when the report would fail the quality gate."""
    with (
        patch("app.main.slack_client.add_reaction", new_callable=AsyncMock),
        patch("app.main.slack_client.get_thread_messages", new_callable=AsyncMock,
              return_value=[{"text": "something broke", "subtype": None}]),
        patch("app.main.slack_client.get_message_permalink", new_callable=AsyncMock,
              return_value=None),
        patch("app.main.slack_client.post_message", new_callable=AsyncMock) as mock_post,
        patch("app.main.bug_extractor.extract_from_thread", new_callable=AsyncMock,
              return_value=low_quality_bug),
        patch("app.main.duplicate_detector.check_duplicate", new_callable=AsyncMock,
              return_value=DuplicateResult(is_duplicate=False, similarity_score=0.0)),
        patch("app.main.github_client.create_issue", new_callable=AsyncMock,
              return_value={"number": 99, "html_url": "https://github.com/org/repo/issues/99"}),
        patch("app.main.github_client.format_issue_body", return_value="body"),
        patch("app.main.github_client.build_labels", return_value=["bug", "severity:critical"]),
    ):
        await _process_thread("C123456", "1000000000.000001", run_quality_gate=False)

    confirmation = mock_post.call_args[0][2]
    assert "https://github.com/org/repo/issues/99" in confirmation


# ---------------------------------------------------------------------------
# Path 2 — quality gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path2_quality_gate_blocks_low_score(low_quality_bug):
    """Low-quality report → feedback posted to Slack, no GitHub issue created."""
    with (
        patch("app.main.slack_client.add_reaction", new_callable=AsyncMock),
        patch("app.main.slack_client.get_thread_messages", new_callable=AsyncMock,
              return_value=[{"text": "bug", "subtype": None}]),
        patch("app.main.slack_client.get_message_permalink", new_callable=AsyncMock,
              return_value=None),
        patch("app.main.slack_client.post_message", new_callable=AsyncMock) as mock_post,
        patch("app.main.bug_extractor.extract_from_thread", new_callable=AsyncMock,
              return_value=low_quality_bug),
        patch("app.main.github_client.create_issue", new_callable=AsyncMock) as mock_create,
    ):
        await _process_thread("C999", "1000000000.000001", run_quality_gate=True)

    mock_create.assert_not_called()
    mock_post.assert_called_once()
    feedback = mock_post.call_args[0][2]
    assert "scored" in feedback
    assert "Missing information" in feedback


# ---------------------------------------------------------------------------
# Path 2 — authorization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthorized_user_is_ignored():
    """Reaction from a user not in AUTHORIZED_SLACK_USERS is silently dropped."""
    event = ReactionEvent(
        type="reaction_added",
        user="U_OUTSIDER",
        reaction="bug",
        item=ReactionItem(type="message", channel="C999", ts="123.456"),
    )

    with patch("app.main._process_thread", new_callable=AsyncMock) as mock_process:
        await handle_reaction_event(event)

    mock_process.assert_not_called()


@pytest.mark.asyncio
async def test_authorized_user_triggers_pipeline():
    """Reaction from an authorized user invokes _process_thread with the quality gate."""
    # U123 is in AUTHORIZED_SLACK_USERS="U123,U456" (set in conftest)
    event = ReactionEvent(
        type="reaction_added",
        user="U123",
        reaction="bug",
        item=ReactionItem(type="message", channel="C999", ts="123.456"),
    )

    with patch("app.main._process_thread", new_callable=AsyncMock) as mock_process:
        await handle_reaction_event(event)

    mock_process.assert_called_once_with("C999", "123.456", run_quality_gate=True)


@pytest.mark.asyncio
async def test_non_bug_reaction_is_ignored():
    """Reactions other than :bug: are silently ignored."""
    event = ReactionEvent(
        type="reaction_added",
        user="U123",
        reaction="thumbsup",
        item=ReactionItem(type="message", channel="C999", ts="123.456"),
    )

    with patch("app.main._process_thread", new_callable=AsyncMock) as mock_process:
        await handle_reaction_event(event)

    mock_process.assert_not_called()


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_comments_existing_issue(good_bug):
    """Duplicate bug → comment added to existing issue, no new issue created."""
    dup = DuplicateResult(
        is_duplicate=True,
        similarity_score=0.95,
        existing_issue_number=10,
        existing_issue_url="https://github.com/org/repo/issues/10",
    )

    with (
        patch("app.main.slack_client.add_reaction", new_callable=AsyncMock),
        patch("app.main.slack_client.get_thread_messages", new_callable=AsyncMock,
              return_value=[{"text": "same login bug", "subtype": None}]),
        patch("app.main.slack_client.get_message_permalink", new_callable=AsyncMock,
              return_value="https://slack.com/link"),
        patch("app.main.slack_client.post_message", new_callable=AsyncMock) as mock_post,
        patch("app.main.bug_extractor.extract_from_thread", new_callable=AsyncMock,
              return_value=good_bug),
        patch("app.main.duplicate_detector.check_duplicate", new_callable=AsyncMock,
              return_value=dup),
        patch("app.main.github_client.add_comment", new_callable=AsyncMock) as mock_comment,
        patch("app.main.github_client.create_issue", new_callable=AsyncMock) as mock_create,
    ):
        await _process_thread("C123456", "1000000000.000001", run_quality_gate=False)

    mock_create.assert_not_called()
    mock_comment.assert_called_once()
    assert mock_comment.call_args[0][0] == 10
    assert "https://github.com/org/repo/issues/10" in mock_post.call_args[0][2]
