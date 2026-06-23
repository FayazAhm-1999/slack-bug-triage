"""Quality scoring for extracted bug reports.

Pure, synchronous, deterministic — no I/O, no external dependencies.
Each field worth 20 points; threshold is 80 (4 of 5 fields populated).
"""

from app.models import BugReport, QualityResult

_THRESHOLD = 80


def score_bug_report(bug: BugReport) -> QualityResult:
    score = 0
    missing: list[str] = []

    checks = [
        (bug.title.strip(), "title"),
        (bug.summary.strip(), "summary"),
        (bool(bug.reproduction_steps), "reproduction steps"),
        (bug.expected_behavior.strip(), "expected behavior"),
        (bug.actual_behavior.strip(), "actual behavior"),
    ]

    for present, field_name in checks:
        if present:
            score += 20
        else:
            missing.append(field_name)

    return QualityResult(
        quality_score=score,
        passed=score >= _THRESHOLD,
        missing_fields=missing,
    )
