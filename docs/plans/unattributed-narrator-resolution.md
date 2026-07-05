# Plan: Resolve `speaker_id=None` to the reserved narrator at synthesis

## Goal
Make the synthesize/voice-resolution layer treat a segment's `speaker_id=None` as the
project's reserved narrator (the rest of the codebase already does), so unattributed quotes
render in the narrator's voice and are voiceable headlessly via `assign-voice --rest` —
deleting the `<unattributed>` sentinel that hard-blocked synthesis with no CLI fix.

## Pipeline stage
Affects **synthesize** (`audio/synthesize.py` resolution seams) and the **review gate**
(`review/gate.py` reuses `unresolved_voices` verbatim, so fixing the precheck fixes the gate
message). No state/schema change; resumability is unaffected — this is a pure resolution-logic
correction. `schema_version` stays put.

## Design decisions

### 1. Narrator-helper placement (layering)
Add a pure query to the **domain** layer:

```python
# src/casttrophizer/domain/models.py
def find_narrator(project: Project) -> Speaker | None:
    """Return the project's reserved narrator (first NARRATOR-role speaker), or None."""
```

Rationale:
- `audio/synthesize.py` already imports from `domain.models`; it must NOT import
  `attribution/`. Domain is the shared lower layer both `audio/` and `attribution/` sit on,
  so it is the only placement that keeps the layering clean without a new dependency edge.
- Identity is by `role == SpeakerRole.NARRATOR` — exactly the predicate `ensure_narrator`
  already uses (not by name), so behavior matches. `models.py` already imports `SpeakerRole`.
- **Single source of truth:** refactor `attribution/policy.py::ensure_narrator` to
  `return find_narrator(project) or <create>` so the lookup rule cannot drift.
- `models.py`'s "pure value objects, no I/O" rule is preserved: this is an in-memory,
  side-effect-free query, not I/O. (A dedicated `domain/queries.py` is the alternative if the
  team prefers to keep `models.py` strictly dataclasses; recommendation is `models.py` — one
  trivial function does not warrant a new module.)

### 2. `--unattributed-to <speaker>` CLI flag — recommendation: **do NOT add**
The fix makes it unnecessary: None renders as narrator automatically (headless succeeds), and
`--rest` voices the narrator. Interactive routing of an unattributed quote to a *specific
character* is exactly what the future per-line review UI owns (and `set_segment_speaker`
already supports it programmatically). Adding the flag now is speculative scope for a flow the
review UI will replace. If a headless override is ever needed before the UI lands, revisit.

### 3. Defensive: no narrator Speaker exists at all
Shouldn't happen post-attribution (`ensure_narrator` runs), but must not crash. When
`find_narrator` returns None and a renderable None segment exists:
- `unresolved_voices` reports the name `"narrator"` (clear, not `<unattributed>`).
- `unresolved_speakers` cannot include it (no Speaker object) — acceptable for a can't-happen
  case; documented.
- `synthesize_chapter` resolves such a segment to no clip → existing flag-and-continue path
  marks it `FAILED` (no crash).

## Affected / new files
- `src/casttrophizer/domain/models.py` — add `find_narrator`; fix `Segment.speaker_id` comment
  (line 67) to note None resolves to the reserved narrator at render.
- `src/casttrophizer/attribution/policy.py` — refactor `ensure_narrator` to reuse
  `find_narrator` (no behavior change).
- `src/casttrophizer/audio/synthesize.py` — the core change (below).
- `src/casttrophizer/cli.py` — docstring fix in `_cmd_assign_voice_rest` (drop the
  `<unattributed>`-excluded sentence). Gate hint block (~497-507) needs **no logic change**:
  narrator is a real Speaker named `"narrator"`, so it now lists by name/index automatically.
- `src/casttrophizer/review/actions.py` — update `set_segment_speaker` docstring/comment
  (lines ~132-136): None no longer "does NOT mean narrator"; it renders as narrator at synth.
- `src/casttrophizer/review/gate.py` — no code change; verify criterion 3 wording still true
  (narrator now correctly counted). Optionally refresh the criterion-3 docstring example.

## Ordered tasks
1. **Add `find_narrator(project) -> Speaker | None`** to `domain/models.py`; update the
   `Segment.speaker_id` comment. (No import cycle: models imports only enums/ids.)
2. **Refactor `ensure_narrator`** in `attribution/policy.py` to delegate to `find_narrator`.
   Confirm all `attribution` tests still pass unchanged.
3. **Rework `audio/synthesize.py`:**
   - `_referenced_speaker_ids`: look up `narrator = find_narrator(project)` once; for a
     renderable segment, use `segment.speaker_id if not None else narrator.id` (skip the
     contribution only when narrator is None). Result now includes `narrator.id` whenever a
     renderable None segment exists and a narrator exists.
   - `unresolved_voices`: delete the `<unattributed>` sentinel block (lines 128-131). Add the
     **defensive** branch: if `find_narrator` is None AND a renderable None segment exists,
     `missing.setdefault("narrator", None)`. Update docstring (remove the `<unattributed>`
     paragraph; state None resolves to narrator).
   - `unresolved_speakers`: no logic change needed — it iterates `_referenced_speaker_ids`,
     which now yields `narrator.id`; the narrator Speaker is therefore included when unvoiced.
     Delete the docstring paragraph about excluding the sentinel.
   - **Delete `_has_unattributed_renderable_segment`** (replace its one remaining use — the
     defensive branch above — with a small inline `any(...)` scan or a clearly-named
     `_has_renderable_none_segment`; do not reintroduce sentinel semantics).
   - `synthesize_chapter`: compute `narrator = find_narrator(project)` once at the top; change
     line 228 to resolve None → narrator id:
     `sid = seg.speaker_id if seg.speaker_id is not None else (narrator.id if narrator else None)`
     then `speaker = speakers.get(sid) if sid else None`. The existing `clip_path is None`
     flag-and-continue path already handles both the race and the no-narrator edge.
   - Sweep docstrings in this module for `<unattributed>` and remove.
4. **CLI docstring fix** in `_cmd_assign_voice_rest`. Verify (do not change) the run-gate hint
   loop lists the narrator by name/index when unvoiced.
5. **`review/actions.py`** `set_segment_speaker` docstring/comment update.
6. **`review/gate.py`** verify + optionally refresh criterion-3 docstring example.

## Interfaces & data shapes
- New: `find_narrator(project: Project) -> Speaker | None` (domain).
- Unchanged signatures: `unresolved_voices(project) -> list[str]`,
  `unresolved_speakers(project) -> list[Speaker]`, `synthesize_chapter(...)`. Only their
  internal resolution and returned *contents* change. No provider-boundary
  (`SynthesisRequest`/`TTSProvider`/LLM) shape changes. No persisted schema change.

## Threading note
No change to threading. All touched functions are the same pure/off-main-thread synthesis
helpers already driven by the `SynthesizeStage` worker; nothing new runs on the Qt main thread.

## Constraints verified
- **NEEDS_REVIEW gate unchanged:** `attribute.py` still stamps unattributed quotes
  `NEEDS_REVIEW` (lines 118-132), and `gate.py` criterion 1 still blocks on it. This change
  touches only voice resolution, orthogonal to `review_status`. Unattributed-but-flagged
  segments remain blocked from synthesis unless `--auto-accept`/explicit review clears them.
- **Layering:** `audio/synthesize.py` imports `find_narrator` from `domain.models`, not from
  `attribution/`. No new dependency edge.
- **Read-only inputs / no schema bump:** unaffected.

## Risks & open questions
- **`review_ready_project` fixture has a voiced narrator**, so the flipped sentinel test's
  planted None segment now resolves to a *voiced* narrator and drops out of both lists — the
  test must be rewritten around that (see below), plus a companion unvoiced-narrator test.
- **Assemble stage** stitches by `audio_cache_key`, not by speaker resolution, so None
  segments there (`test_assemble_*`) are unaffected — verify, don't change.
- Confirm no other test asserts "synthesize FAILED because a None segment exists" — grep
  `<unattributed>` (only two test files today) and any precheck-on-None assertion.

## Verification strategy
Gates: `ruff`, `black --check`, `mypy --strict`, `pytest` — all green, no real TTS/LLM calls.

**Existing tests that flip:**
- `tests/audio/test_unresolved_speakers.py::test_excludes_unattributed_sentinel` — rewrite.
  New intent (`test_none_segment_resolves_to_voiced_narrator`): plant a renderable None segment
  in `review_ready_project` (narrator voiced) → assert `"<unattributed>"` never appears and
  narrator is NOT in `unresolved_voices`/`unresolved_speakers`. Update the module docstring
  (lines 1-6) that describes the sentinel.
- `tests/review/test_actions_extra.py::test_set_segment_speaker_none_leaves_unresolved` —
  assertions still hold (model still stores None); update the name/docstring so it no longer
  claims None "leaves unattributed"/"does NOT mean narrator" (rename to
  `test_set_segment_speaker_none_stores_none` and note None renders as narrator at synth).

**New tests:**
- `unresolved_speakers`/`unresolved_voices` — None segment + **unvoiced** narrator (unassign
  narrator's clip in `review_ready_project`, plant a None segment): assert `"narrator"` in
  `unresolved_voices` AND the narrator `Speaker` in `unresolved_speakers` (proves `--rest` can
  now fix it). This is the headless-fix regression guard for the reported bug.
- `synthesize_chapter` integration (FakeTTS): a renderable None segment renders using the
  **narrator's** clip — assert the captured `SynthesisRequest.voice_clip_path` equals the
  narrator's clip path and the segment ends `COMPLETED`.
- Gate message: with narrator unvoiced + a None segment, `describe_blockers(review_blockers(p))`
  contains `voices needed for: narrator` (proves the gate string is fixed, not `<unattributed>`).
- Defensive no-narrator: a project with a renderable None segment and no NARRATOR speaker →
  `unresolved_voices` contains `"narrator"`, `synthesize_chapter` marks that segment `FAILED`
  (no crash), and `find_narrator` returns None.
- `find_narrator` unit: returns the NARRATOR-role speaker; None on an empty/narrator-less
  project; and `ensure_narrator` still creates-then-finds one.

The tester must prove, end-to-end against FakeTTS: (a) None + voiced narrator → renders in the
narrator voice, absent from both unresolved lists; (b) None + unvoiced narrator → narrator
reported by name AND present in `unresolved_speakers`, and `assign-voice --rest` clears it.

---
**Handoff — build first:** `find_narrator(project)` in `domain/models.py` and the
`ensure_narrator` refactor (task 1-2), then rework `audio/synthesize.py` (task 3). The two
synthesize seams (`unresolved_*` + `synthesize_chapter`) are the crux; everything else is
docstring/wording cleanup and tests.
