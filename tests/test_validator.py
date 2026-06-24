"""Tests for validator.py — pure functions, no mocks required."""

from app.validator import score_bug_report


def test_perfect_score(make_bug):
    result = score_bug_report(make_bug())
    assert result.quality_score == 100
    assert result.passed is True
    assert result.missing_fields == []


def test_missing_title(make_bug):
    result = score_bug_report(make_bug(title=""))
    assert result.quality_score == 80
    assert result.passed is True
    assert "title" in result.missing_fields


def test_missing_two_fields_fails(make_bug):
    result = score_bug_report(make_bug(title="", summary=""))
    assert result.quality_score == 60
    assert result.passed is False
    assert "title" in result.missing_fields
    assert "summary" in result.missing_fields


def test_missing_reproduction_steps(make_bug):
    result = score_bug_report(make_bug(reproduction_steps=[]))
    assert result.quality_score == 80
    assert "reproduction steps" in result.missing_fields


def test_all_missing_except_severity(make_bug):
    result = score_bug_report(make_bug(
        title="",
        summary="",
        reproduction_steps=[],
        expected_behavior="",
        actual_behavior="",
    ))
    assert result.quality_score == 0
    assert result.passed is False
    assert len(result.missing_fields) == 5


def test_whitespace_only_title_counts_as_missing(make_bug):
    """A title containing only whitespace should be treated as missing."""
    result = score_bug_report(make_bug(title="   "))
    assert "title" in result.missing_fields
