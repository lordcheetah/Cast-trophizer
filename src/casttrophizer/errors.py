"""Exception hierarchy for Cast-trophizer.

All library-raised errors derive from :class:`CasttrophizerError` so callers (UI,
tests) can catch the whole family with one ``except``. Subtypes group failures by the
subsystem that raised them, keeping handling code provider- and stage-agnostic.
"""

from __future__ import annotations

__all__ = [
    "CasttrophizerError",
    "ConfigError",
    "WorkspaceError",
    "SerializationError",
    "MigrationError",
    "ProviderError",
    "LLMProviderError",
    "TTSProviderError",
    "EbookParseError",
    "PipelineError",
    "StageError",
    "StoppedError",
    "AssemblyError",
]


class CasttrophizerError(Exception):
    """Base class for every error raised by Cast-trophizer."""


class ConfigError(CasttrophizerError):
    """Invalid or missing configuration (bad provider selection, missing paths, etc.)."""


class WorkspaceError(CasttrophizerError):
    """A workspace could not be read, written, or initialized."""


class SerializationError(CasttrophizerError):
    """A domain object could not be (de)serialized to/from its on-disk form."""


class MigrationError(SerializationError):
    """A persisted ``schema_version`` cannot be migrated to the current schema."""


class ProviderError(CasttrophizerError):
    """Base class for external-provider failures (LLM / TTS)."""


class LLMProviderError(ProviderError):
    """An LLM provider failed or is unavailable."""


class TTSProviderError(ProviderError):
    """A TTS provider failed or is unavailable."""


class EbookParseError(CasttrophizerError):
    """An ebook could not be parsed."""


class PipelineError(CasttrophizerError):
    """Base class for pipeline-level failures."""


class StageError(PipelineError):
    """A pipeline stage failed during execution."""


class StoppedError(PipelineError):
    """Raised (or signalled) when a cooperative stop was requested mid-stage.

    Stages normally return a STOPPED :class:`~casttrophizer.pipeline.stage.StageResult`
    rather than raising; this exists for code paths that prefer an exception.
    """


class AssemblyError(CasttrophizerError):
    """The final audiobook could not be assembled (e.g. ffmpeg failure)."""
