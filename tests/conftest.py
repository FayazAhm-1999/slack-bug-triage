"""Shared test configuration.

Sets environment variables at module level — before any app module is imported —
so that Settings() at config.py module level reads test values instead of
looking for a .env file.
"""

import os

# Must happen before any `from app.*` import across all test files,
# since config.py executes Settings() at module level.
_TEST_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_SIGNING_SECRET": "test_signing_secret",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "GITHUB_TOKEN": "ghp_test",
    "GITHUB_REPO": "test-org/test-repo",
    "AUTHORIZED_SLACK_USERS": "U123,U456",
    "BUG_CHANNEL_ID": "C123456",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

import pytest  # noqa: E402 — must come after env setup
from app.models import BugReport  # noqa: E402


@pytest.fixture()
def make_bug():
    """Factory fixture that returns a fully-populated BugReport.

    Override any field by passing keyword arguments::

        def test_foo(make_bug):
            bug = make_bug(title="")  # title missing, all others present
    """
    def _factory(**overrides) -> BugReport:
        defaults = dict(
            title="Checkout fails for Visa cards",
            summary="Users with Visa cards see a 500 error on checkout.",
            severity="critical",
            affected_component="payments/checkout",
            reproduction_steps=["Add item to cart", "Enter Visa card details", "Click Pay"],
            expected_behavior="Order is confirmed",
            actual_behavior="500 Internal Server Error is returned",
        )
        return BugReport(**{**defaults, **overrides})
    return _factory
