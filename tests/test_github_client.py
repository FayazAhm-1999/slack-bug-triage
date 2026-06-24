"""Tests for github_client pure functions."""

from app.github_client import format_issue_body


def test_format_issue_body(make_bug):
    bug = make_bug()
    with_url = format_issue_body(bug, "https://slack.com/thread/123")
    without_url = format_issue_body(bug, None)

    # All required sections are present
    for section in ("## Summary", "## Severity", "## Affected Component",
                    "## Reproduction Steps", "## Expected Behavior",
                    "## Actual Behavior", "## Source Slack Thread"):
        assert section in with_url

    # Slack URL is embedded as a link; falls back when absent
    assert "https://slack.com/thread/123" in with_url
    assert "_Not available_" in without_url

    # Reproduction steps are numbered
    assert "1. Add item to cart" in with_url
    assert "2. Enter Visa card details" in with_url
