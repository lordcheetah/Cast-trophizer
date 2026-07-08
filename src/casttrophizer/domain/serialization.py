"""Hand-written (de)serialization for the domain model.

Per the project decisions this uses **stdlib dataclasses + manual to_dict/from_dict**
with a ``schema_version`` — no pydantic. Centralizing it here means the on-disk shape
is versioned in one place and a future switch to a different mechanism stays isolated.

The public entry points are :func:`project_to_dict` and :func:`project_from_dict`.
The round-trip is lossless: ``project_from_dict(project_to_dict(p)) == p``.

Versioning: ``project_to_dict`` always stamps :data:`CURRENT_SCHEMA_VERSION`.
``project_from_dict`` dispatches on the stored version through :func:`_migrate`; an
unknown/future version raises :class:`~casttrophizer.errors.MigrationError`.
"""

from __future__ import annotations

from typing import Any

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, VoiceCategory
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Segment,
    Speaker,
    TextSuggestion,
    VoiceClip,
)
from casttrophizer.errors import MigrationError, SerializationError

__all__ = ["CURRENT_SCHEMA_VERSION", "project_to_dict", "project_from_dict"]

#: Current on-disk schema version. Bump when the persisted shape changes and add a
#: migration step in :func:`_migrate`.
#:
#: History:
#:   v1 -> v2: added ``Speaker.category`` (:class:`VoiceCategory`); pre-v2 speakers with no
#:             ``category`` key default to ``unknown`` (see :func:`_migrate_v1_to_v2`).
#:   v2 -> v3: added ``Segment.audio_seed`` (per-segment render seed, re-rolled on an audio-review
#:             regenerate); pre-v3 segments default it to ``None`` (see :func:`_migrate_v2_to_v3`).
#:             ``None`` is NOT folded into the AudioCache key, so upgrading a v2 project keeps every
#:             cached WAV valid (no wholesale invalidation).
CURRENT_SCHEMA_VERSION = 3


# --------------------------------------------------------------------------- #
# to_dict
# --------------------------------------------------------------------------- #
def _suggestion_to_dict(s: TextSuggestion) -> dict[str, Any]:
    return {
        "id": s.id,
        "original": s.original,
        "suggested": s.suggested,
        "reason": s.reason,
        "confidence": s.confidence,
        "status": str(s.status),
    }


def _segment_to_dict(seg: Segment) -> dict[str, Any]:
    return {
        "id": seg.id,
        "text": seg.text,
        "speaker_id": seg.speaker_id,
        "role": str(seg.role),
        "confidence": seg.confidence,
        "review_status": str(seg.review_status),
        "audio_cache_key": seg.audio_cache_key,
        "audio_status": str(seg.audio_status),
        "audio_seed": seg.audio_seed,
    }


def _line_to_dict(line: Line) -> dict[str, Any]:
    return {
        "id": line.id,
        "chapter_id": line.chapter_id,
        "order": line.order,
        "text": line.text,
        "segments": [_segment_to_dict(s) for s in line.segments],
        "suggestions": [_suggestion_to_dict(s) for s in line.suggestions],
    }


def _chapter_to_dict(ch: Chapter) -> dict[str, Any]:
    return {
        "id": ch.id,
        "order": ch.order,
        "title": ch.title,
        "lines": [_line_to_dict(line) for line in ch.lines],
    }


def _speaker_to_dict(sp: Speaker) -> dict[str, Any]:
    return {
        "id": sp.id,
        "name": sp.name,
        "role": str(sp.role),
        "voice_clip_id": sp.voice_clip_id,
        "category": str(sp.category),
    }


def _voice_clip_to_dict(vc: VoiceClip) -> dict[str, Any]:
    return {"id": vc.id, "source_path": vc.source_path, "label": vc.label}


def _book_to_dict(book: Book) -> dict[str, Any]:
    return {
        "title": book.title,
        "author": book.author,
        "source_ebook_path": book.source_ebook_path,
        "cover_image_path": book.cover_image_path,
        "chapters": [_chapter_to_dict(ch) for ch in book.chapters],
    }


def project_to_dict(project: Project) -> dict[str, Any]:
    """Serialize a :class:`Project` to a JSON-ready dict, stamping the schema version."""
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "id": project.id,
        "name": project.name,
        "workspace_dir": project.workspace_dir,
        "book": _book_to_dict(project.book),
        "speakers": [_speaker_to_dict(sp) for sp in project.speakers],
        "voice_clips": [_voice_clip_to_dict(vc) for vc in project.voice_clips],
        "stage_status": {k: str(v) for k, v in project.stage_status.items()},
        "tts_params": dict(project.tts_params),
    }


# --------------------------------------------------------------------------- #
# from_dict
# --------------------------------------------------------------------------- #
def _suggestion_from_dict(d: dict[str, Any]) -> TextSuggestion:
    return TextSuggestion(
        id=d["id"],
        original=d["original"],
        suggested=d["suggested"],
        reason=d["reason"],
        confidence=d["confidence"],
        status=ReviewStatus(d["status"]),
    )


def _segment_from_dict(d: dict[str, Any]) -> Segment:
    return Segment(
        id=d["id"],
        text=d["text"],
        speaker_id=d["speaker_id"],
        role=SpeakerRole(d["role"]),
        confidence=d["confidence"],
        review_status=ReviewStatus(d["review_status"]),
        audio_cache_key=d["audio_cache_key"],
        audio_status=ReviewStatus(d["audio_status"]),
        # ``.get`` so a pre-migration (v2) dict — or a re-serialize before ``_migrate`` ran —
        # is safe; ``_migrate_v2_to_v3`` sets the key explicitly for a fully-migrated dict.
        audio_seed=d.get("audio_seed"),
    )


def _line_from_dict(d: dict[str, Any]) -> Line:
    return Line(
        id=d["id"],
        chapter_id=d["chapter_id"],
        order=d["order"],
        text=d["text"],
        segments=[_segment_from_dict(s) for s in d["segments"]],
        suggestions=[_suggestion_from_dict(s) for s in d["suggestions"]],
    )


def _chapter_from_dict(d: dict[str, Any]) -> Chapter:
    return Chapter(
        id=d["id"],
        order=d["order"],
        title=d["title"],
        lines=[_line_from_dict(line) for line in d["lines"]],
    )


def _speaker_from_dict(d: dict[str, Any]) -> Speaker:
    return Speaker(
        id=d["id"],
        name=d["name"],
        role=SpeakerRole(d["role"]),
        voice_clip_id=d["voice_clip_id"],
        category=VoiceCategory(d["category"]),  # present post-migration (v1->v2 defaults it)
    )


def _voice_clip_from_dict(d: dict[str, Any]) -> VoiceClip:
    return VoiceClip(id=d["id"], source_path=d["source_path"], label=d["label"])


def _book_from_dict(d: dict[str, Any]) -> Book:
    return Book(
        title=d["title"],
        author=d["author"],
        source_ebook_path=d["source_ebook_path"],
        cover_image_path=d["cover_image_path"],
        chapters=[_chapter_from_dict(ch) for ch in d["chapters"]],
    )


def project_from_dict(data: dict[str, Any]) -> Project:
    """Deserialize a dict (as produced by :func:`project_to_dict`) into a Project.

    Migrates older schema versions up to the current one. Raises
    :class:`~casttrophizer.errors.MigrationError` for an unknown/future version and
    :class:`~casttrophizer.errors.SerializationError` for a structurally invalid dict.
    """
    try:
        version = int(data["schema_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError("missing or invalid 'schema_version'") from exc

    data = _migrate(data, version)

    try:
        return Project(
            schema_version=CURRENT_SCHEMA_VERSION,
            id=data["id"],
            name=data["name"],
            workspace_dir=data["workspace_dir"],
            book=_book_from_dict(data["book"]),
            speakers=[_speaker_from_dict(sp) for sp in data["speakers"]],
            voice_clips=[_voice_clip_from_dict(vc) for vc in data["voice_clips"]],
            stage_status={k: ReviewStatus(v) for k, v in data["stage_status"].items()},
            tts_params=dict(data["tts_params"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError(f"malformed project data: {exc}") from exc


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #
def _migrate(data: dict[str, Any], version: int) -> dict[str, Any]:
    """Migrate ``data`` from its stored ``version`` up to :data:`CURRENT_SCHEMA_VERSION`.

    A version-step chain: each bump adds a step that transforms ``version -> version + 1``.
    A future/unknown version, or a version with no path to the current one, is a hard error
    rather than a silent best-effort load.
    """
    if version > CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"project schema_version {version} is newer than supported "
            f"{CURRENT_SCHEMA_VERSION}; upgrade Cast-trophizer to open this workspace"
        )
    if version < 1:
        raise MigrationError(
            f"no migration path from schema_version {version} to {CURRENT_SCHEMA_VERSION}"
        )
    if version == 1:
        data = _migrate_v1_to_v2(data)
        version = 2
    if version == 2:
        data = _migrate_v2_to_v3(data)
        version = 3
    if version != CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"no migration path from schema_version {version} to {CURRENT_SCHEMA_VERSION}"
        )
    return data


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: stamp every speaker with ``category=unknown`` (the field's new default).

    Pre-feature projects had no ``category`` key; ``unknown`` is covered by ``--default``
    under ``assign-voice --rest``, so a migrated project can be fully voiced immediately.
    """
    for speaker in data.get("speakers", []):
        speaker.setdefault("category", str(VoiceCategory.UNKNOWN))
    return data


def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """v2 -> v3: stamp every segment with ``audio_seed=None`` (the field's new default).

    Pre-feature segments had no ``audio_seed`` key. ``None`` means "use the global seed" and is
    NOT folded into the :class:`~casttrophizer.workspace.audio_cache.AudioCache` key, so a migrated
    v2 project keeps every cached WAV valid — no wholesale re-render. ``setdefault`` mirrors
    :func:`_migrate_v1_to_v2`, so a dict that (unexpectedly) already carries a seed is preserved.
    """
    for chapter in data.get("book", {}).get("chapters", []):
        for line in chapter.get("lines", []):
            for segment in line.get("segments", []):
                segment.setdefault("audio_seed", None)
    return data
