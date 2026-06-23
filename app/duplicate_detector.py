"""Duplicate detection via TF-IDF cosine similarity.

Design rationale — TF-IDF over neural embeddings:
  - No model download, no torch dependency, runs in milliseconds in-process.
  - Bug reports use specific technical vocabulary ("NullPointerException", "404
    on /checkout") that TF-IDF matches well via exact term overlap.
  - The 50-issue corpus is small; brute-force comparison is fast enough forever.
  - Upgrade path: replace the vectorizer call with sentence-transformers
    all-MiniLM-L6-v2 embeddings — the DuplicateResult interface stays identical.
"""

import logging

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app import github_client
from app.config import settings
from app.models import BugReport, DuplicateResult

logger = logging.getLogger(__name__)


def _bug_to_document(bug: BugReport) -> str:
    """Concatenate all text fields to give TF-IDF a rich document to work with."""
    steps = " ".join(bug.reproduction_steps)
    return f"{bug.title} {bug.summary} {steps} {bug.expected_behavior} {bug.actual_behavior}"


def _issue_to_document(issue: dict) -> str:
    return f"{issue.get('title', '')} {issue.get('body') or ''}"


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

    try:
        new_doc = _bug_to_document(bug)
        existing_docs = [_issue_to_document(i) for i in issues]

        # Fit on the full corpus so vocabulary is consistent across all documents
        vectorizer = TfidfVectorizer(stop_words="english", min_df=1)
        all_docs = [new_doc] + existing_docs
        matrix = vectorizer.fit_transform(all_docs)

        new_vec = matrix[0]
        existing_matrix = matrix[1:]

        similarities = cosine_similarity(new_vec, existing_matrix).flatten()
        best_idx = int(np.argmax(similarities))
        best_score = float(similarities[best_idx])

        if best_score >= settings.duplicate_threshold:
            matched = issues[best_idx]
            return DuplicateResult(
                is_duplicate=True,
                similarity_score=best_score,
                existing_issue_number=matched["number"],
                existing_issue_url=matched["html_url"],
            )

        return DuplicateResult(is_duplicate=False, similarity_score=best_score)

    except Exception:
        logger.exception("Duplicate detection computation failed")
        return DuplicateResult(is_duplicate=False, similarity_score=0.0)
