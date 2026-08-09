"""Unit tests for the pure text-suggestion view-model derivation (no presenter, no Qt).

``suggestion_rows`` flattens ``chapters -> lines -> suggestions`` in order; ``is_pending``
mirrors the gate's criterion-2 predicate (only PENDING blocks). Asserted against the
``review_ready_project`` fixture, which carries one AUTO_APPLIED (resolved) and one PENDING
suggestion.
"""

from __future__ import annotations

from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.models import Project
from casttrophizer.review.suggestion_view import pending_count, suggestion_rows


def test_suggestion_rows_shape_and_order(review_ready_project: Project) -> None:
    rows = suggestion_rows(review_ready_project)
    # Flattened in chapter -> line -> suggestion order: the AUTO_APPLIED one first, PENDING next.
    assert [r.original for r in rows] == ["Allice", "Bbo"]
    assert [r.suggested for r in rows] == ["Alice", "Bob"]
    assert [r.is_pending for r in rows] == [False, True]
    # line_text is the current full-line context (not the token).
    assert rows[0].line_text == '"Hello," said Alice.'
    assert rows[1].line_text == '"Hi," Bob replied.'
    # chapter/line coordinates carried through.
    assert rows[1].chapter_index == 0
    assert rows[1].line_order == 1


def test_reason_and_confidence_carried_through(review_ready_project: Project) -> None:
    pending = suggestion_rows(review_ready_project)[1]
    assert pending.reason == "spellcheck"
    assert pending.confidence == 0.6
    assert pending.status == ReviewStatus.PENDING


def test_resolved_status_is_not_pending(review_ready_project: Project) -> None:
    resolved = suggestion_rows(review_ready_project)[0]
    assert resolved.status == ReviewStatus.AUTO_APPLIED
    assert resolved.is_pending is False


def test_pending_count(review_ready_project: Project) -> None:
    assert pending_count(suggestion_rows(review_ready_project)) == 1
