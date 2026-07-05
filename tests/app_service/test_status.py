"""``stage_status_rows`` / ``next_stage_name`` — rows, statuses, and the ``is_next`` flag."""

from __future__ import annotations

from casttrophizer.app_service.deps import AppServiceDeps
from casttrophizer.app_service.pipeline_service import build_pipeline
from casttrophizer.app_service.status import next_stage_name, stage_status_rows
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project


def test_rows_on_fresh_project_flag_parse_as_next(parse_ready_project: Project) -> None:
    pipeline = build_pipeline(AppServiceDeps())
    rows = stage_status_rows(parse_ready_project, pipeline)

    assert [row.name for row in rows] == list(StageName)  # six rows, in order
    assert all(row.status is None for row in rows)  # nothing recorded yet
    next_rows = [row for row in rows if row.is_next]
    assert len(next_rows) == 1
    assert next_rows[0].name == StageName.PARSE
    assert next_stage_name(parse_ready_project, pipeline) == StageName.PARSE


def test_rows_mid_pipeline_flag_review_as_next(review_ready_project: Project) -> None:
    pipeline = build_pipeline(AppServiceDeps())
    rows = stage_status_rows(review_ready_project, pipeline)
    by_name = {row.name: row for row in rows}

    assert by_name[StageName.PARSE].status == ReviewStatus.COMPLETED
    assert by_name[StageName.ATTRIBUTE].status == ReviewStatus.COMPLETED
    assert by_name[StageName.REVIEW].status is None
    assert by_name[StageName.REVIEW].is_next
    assert not by_name[StageName.PARSE].is_next
    assert next_stage_name(review_ready_project, pipeline) == StageName.REVIEW
