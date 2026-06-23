"""Tests for validator.py — pure functions, no mocks required."""

from app.models import BugReport
from app.validator import score_bug_report


def _make_bug(**overrides) -> BugReport:
    defaults = dict(
        title="Login page crashes on Safari",
        summary="Users on Safari 17 cannot log in — the page throws a JS error.",
        severity="high",
        affected_component="auth/login",
        reproduction_steps=["Open Safari 17", "Navigate to /login", "Click Sign In"],
        expected_behavior="User is redirected to dashboard",
        actual_behavior="Page throws TypeError and goes blank",
    )
    return BugReport(**{**defaults, **overrides})


def test_perfect_score():
    result = score_bug_report(_make_bug())
    assert result.quality_score == 100
    assert result.passed is True
    assert result.missing_fields == []


def test_missing_title():
    result = score_bug_report(_make_bug(title=""))
    assert result.quality_score == 80
    assert result.passed is True
    assert "title" in result.missing_fields


def test_missing_two_fields_fails():
    result = score_bug_report(_make_bug(title="", summary=""))
    assert result.quality_score == 60
    assert result.passed is False
    assert "title" in result.missing_fields
    assert "summary" in result.missing_fields


def test_missing_reproduction_steps():
    result = score_bug_report(_make_bug(reproduction_steps=[]))
    assert result.quality_score == 80
    assert "reproduction steps" in result.missing_fields


def test_all_missing_except_severity():
    result = score_bug_report(_make_bug(
        title="",
        summary="",
        reproduction_steps=[],
        expected_behavior="",
        actual_behavior="",
    ))
    assert result.quality_score == 0
    assert result.passed is False
    assert len(result.missing_fields) == 5


def test_threshold_boundary_exactly_80():
    # Score of exactly 80 should pass
    result = score_bug_report(_make_bug(title=""))
    assert result.quality_score == 80
    assert result.passed is True
