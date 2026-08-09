"""Shared category-clip resolution for bulk voice assignment (the CLI/UI drift seam).

Both ``castrun assign-voice --rest`` and the PySide6 voice-assignment panel need to bulk-voice
every still-unvoiced referenced speaker by category. This module holds the one implementation
so the two front-ends cannot drift: the CLI's ``_cmd_assign_voice_rest`` and the UI's
``VoicePresenter.bulk_assign_by_category`` both build a plain ``overrides`` mapping (not an
``argparse.Namespace``) and call :func:`plan_bulk_voice` / :func:`apply_bulk_voice`.

Layering: ``app_service`` sits above ``review``/``audio``, so importing :class:`ReviewService`,
:mod:`~casttrophizer.review.actions`, and ``audio.synthesize.unresolved_speakers`` here is fine.
It imports **no** Qt, so ``tests/test_qt_isolation.py`` still covers the module.

The target set is **exactly** ``unresolved_speakers(project)`` — the same predicate the review
gate and the render precheck use — so bulk-assign can never "finish" while the gate still
blocks, nor pointlessly voice an unreferenced speaker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from casttrophizer.audio.synthesize import unresolved_speakers
from casttrophizer.config import AppConfig
from casttrophizer.domain.models import Project, Speaker, VoiceClip
from casttrophizer.review import actions
from casttrophizer.review.service import ReviewService

__all__ = [
    "REST_CATEGORY_KEYS",
    "resolve_category_clip",
    "BulkVoicePlan",
    "plan_bulk_voice",
    "apply_bulk_voice",
]

#: Voice categories with a dedicated per-category override key. ``unknown`` has no key — it
#: resolves straight to ``"default"`` (mirrors the CLI's ``--man/--woman/--boy/--girl`` flags,
#: with ``unknown``/uncovered falling back to ``--default``).
REST_CATEGORY_KEYS = ("man", "woman", "boy", "girl")


def resolve_category_clip(
    category: str, overrides: Mapping[str, str | None], config: AppConfig
) -> str | None:
    """Resolve one voice category to a clip path: override > env default > shared ``default``.

    ``overrides`` carries the per-category keys (``man``/``woman``/``boy``/``girl``) plus
    ``default``. A per-category override wins over the matching ``config.voice_defaults`` env
    default, which wins over the shared ``default`` override (which itself wins over the
    ``config.voice_defaults["default"]`` env default). A category with no key (``unknown``) or no
    per-category value falls back to that default; returns ``None`` when nothing resolves (an
    uncovered category).
    """
    default_path = overrides.get("default") or config.voice_defaults.get("default")
    if category in REST_CATEGORY_KEYS:
        return overrides.get(category) or config.voice_defaults.get(category) or default_path
    return default_path  # unknown / any future keyless category


@dataclass(frozen=True)
class BulkVoicePlan:
    """The pure, dry-run result of resolving bulk-assign targets to clip paths.

    ``assignments`` are the ``(speaker, resolved clip path)`` pairs to voice; ``uncovered`` maps
    a category value to the names of speakers whose category resolved to no clip. A non-empty
    ``uncovered`` MUST be rejected by the caller before :func:`apply_bulk_voice` — never leave a
    referenced speaker unvoiced (that would re-block the gate).
    """

    assignments: list[tuple[Speaker, str]]
    uncovered: dict[str, list[str]]


def plan_bulk_voice(
    project: Project, overrides: Mapping[str, str | None], config: AppConfig
) -> BulkVoicePlan:
    """Resolve every still-unvoiced referenced speaker to a category clip (PURE — no mutation).

    Targets exactly ``unresolved_speakers(project)`` (the gate/render predicate). Each target's
    category is resolved via :func:`resolve_category_clip`; a ``None`` result buckets the speaker
    under ``uncovered`` (by category value), else it joins ``assignments``. No I/O and no
    path-existence check here — that (and the actual registration/assignment) is
    :func:`apply_bulk_voice`'s job.
    """
    assignments: list[tuple[Speaker, str]] = []
    uncovered: dict[str, list[str]] = {}
    for speaker in unresolved_speakers(project):
        category = speaker.category.value
        path = resolve_category_clip(category, overrides, config)
        if path is None:
            uncovered.setdefault(category, []).append(speaker.name)
        else:
            assignments.append((speaker, path))
    return BulkVoicePlan(assignments=assignments, uncovered=uncovered)


def apply_bulk_voice(service: ReviewService, plan: BulkVoicePlan) -> dict[str, int]:
    """Register one shared clip per distinct path, batch-assign the plan, and persist once.

    Registers exactly one :class:`VoiceClip` per **distinct** path (validating each exists →
    :class:`ValueError` before anything is persisted), then ``service.assign_voices(pairs)``
    (a single save). Returns per-category assignment counts.

    The caller MUST reject a non-empty ``plan.uncovered`` first — this function assumes full
    coverage and never leaves a referenced speaker unvoiced.
    """
    project = service.project
    clips_by_path: dict[str, VoiceClip] = {}
    counts: dict[str, int] = {}
    pairs: list[tuple[Speaker, VoiceClip]] = []
    for speaker, path in plan.assignments:
        clip = clips_by_path.get(path)
        if clip is None:
            clip = actions.register_voice_clip(
                project, path, label=f"{speaker.category.value} (default)"
            )
            clips_by_path[path] = clip
        pairs.append((speaker, clip))
        counts[speaker.category.value] = counts.get(speaker.category.value, 0) + 1

    service.assign_voices(pairs)
    return counts
