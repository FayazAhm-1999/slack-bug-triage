"""Tests for github_client pure functions."""

from unittest.mock import patch

# Patch env before config is imported so Settings() doesn't fail without a .env
with patch.dict("os.environ", {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_SIGNING_SECRET": "test_signing_secret",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "GITHUB_TOKEN": "ghp_test",
    "GITHUB_REPO": "test-org/test-repo",
    "AUTHORIZED_SLACK_USERS": "U123,U456",
    "BUG_CHANNEL_ID": "C123456",
}):
    from app.github_client import build_labels, format_issue_body
    from app.models import BugReport


def _make_bug() -> BugReport:
    return BugReport(
        title="Checkout fails for Visa cards",
        summary="Users with Visa cards see a 500 error on checkout.",
        severity="critical",
        affected_component="payments/checkout",
        reproduction_steps=["Add item to cart", "Enter Visa card details", "Click Pay"],
        expected_behavior="Order is confirmed",
        actual_behavior="500 Internal Server Error is returned",
    )


def test_build_labels():
    assert build_labels("high") == ["bug", "severity:high"]
    assert build_labels("critical") == ["bug", "severity:critical"]


def test_format_issue_body_contains_sections():
    body = format_issue_body(_make_bug(), "https://slack.com/thread/123")
    assert "## Summary" in body
    assert "## Severity" in body
    assert "## Affected Component" in body
    assert "## Reproduction Steps" in body
    assert "## Expected Behavior" in body
    assert "## Actual Behavior" in body
    assert "## Source Slack Thread" in body


def test_format_issue_body_includes_slack_url():
    body = format_issue_body(_make_bug(), "https://slack.com/thread/123")
    assert "https://slack.com/thread/123" in body


def test_format_issue_body_no_slack_url():
    body = format_issue_body(_make_bug(), None)
    assert "_Not available_" in body


def test_format_issue_body_numbered_steps():
    body = format_issue_body(_make_bug(), None)
    assert "1. Add item to cart" in body
    assert "2. Enter Visa card details" in body
