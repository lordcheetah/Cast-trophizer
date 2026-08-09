# Plan: Review Stage (`ReviewStage`) — the human gate between `attribute` and `synthesize`

**Goal:** Give the user a way to resolve every flagged attribution, every pending text suggestion,
and every missing voice assignment, so the pipeline can advance from `attribute` to `synthesize`;
the headless `ReviewStage` returns `NEEDS_REVIEW` (the runner halts) until the project is "reviewed
and ready to synthesize," then `COMPLETED`.

Pipeline stage: **`review`** (fourth stage; after `attribute`, before `synthesize`). Unlike every
prior stage, review's *work* is interactive and happens in the PySide6 UI. This plan splits that into
**(A)** a thin, Qt-free, testable **gate** (`ReviewStage`) and **(B)** a set of Qt-free, testable
**review-action operations** the UI calls to mutate the project. The full GUI is scoped as a
**separate follow-on phase** (Section C).

---

## 0. Decisions locked by existing code (do not relitigate)

- The runner (`pipeline/runner.py`) **halts** on `STOPPED / NEEDS_REVIEW / FAILED` (`_HALTING`). So a
  `ReviewStage.run` returning `NEEDS_REVIEW` is exactly the "stop and wait for the human" mechanism —
  no new runner machinery is needed.
- `is_complete` across stages is **purely `stage_status`-driven** (parse/correct/attribute/synthesize
  all do `project.stage_status.get(str(StageName.X)) == COMPLETED`). Review must NOT break that
  contract — see §A.3 for the one subtlety (review's "completeness" is computed from *content*, not a
  recorded flag, because no `run` writes `stage_status[REVIEW]=COMPLETED` until the gate passes).
- `Segment.review_status` is the **attribution** review state; `Segment.audio_status` is the **audio**
  review state (distinct). Pre-synthesis review (this plan) only touches `review_status`,
  `speaker_id`, `role`, `Line.text`, `Line.suggestions`, `Speaker.voice_clip_id`, and
  `project.voice_clips`. Post-synthesis **audio** review (`audio_status`) is explicitly out of scope
  (§C).
- The synthesize stage already has a **fail-fast voice precheck** (`audio/synthesize.py
  unresolved_voices(project)`) that returns the display names of referenced speakers lacking a usable
  voice clip. The review gate **reuses this exact function** so "voices assigned" is asserted
  identically in both places (no second implementation, no drift).
- `AudioCache.key_for(segment, project)` folds in `segment.text`, the resolved `voice_clip_id`, and
  `project.tts_params`. **Editing a segment's text or reassigning a speaker's voice changes that key**
  → synthesize re-renders only the affected segments automatically. The review actions therefore need
  **no explicit cache invalidation** — they just mutate the project; the key *is* the invalidation
  mechanism. (This plan only *notes* the consequence; it does not touch the cache.)
- Serialization (`domain/serialization.py`) already round-trips `Line.text`, `Line.suggestions`,
  `Line.segments` (incl. `review_status`/`speaker_id`/`role`), `project.speakers`
  (incl. `voice_clip_id`), and `project.voice_clips`. **No schema change is required** for any review
  action (§Schema impact). `CURRENT_SCHEMA_VERSION` stays `1`.
- Stage contract (`pipeline/stage.py`): Qt-free, no provider construction, reads/writes only via
  `ctx.store`. `ReviewStage` needs **neither** `ctx.llm` nor `ctx.tts` — it is a pure compute/gate
  stage (the cheapest stage in the pipeline).

---

## A. The headless `ReviewStage` GATE (Qt-free, testable) — precise definition

### A.1 What "ready to synthesize" means — the exact, checkable gate criteria

The project is **review-complete** iff **all** of the following hold over the persisted state:

1. **No unresolved attribution.** No `Segment` anywhere has
   `review_status == ReviewStatus.NEEDS_REVIEW`. (Every low-confidence attribution the attribute
   stage flagged has been moved to `APPROVED` or `REJECTED` by a user action — §B.2.)
2. **No pending text suggestion.** No `TextSuggestion` anywhere has
   `status == ReviewStatus.PENDING`. (Each surfaced suggestion has been `APPROVED`/`AUTO_APPLIED` or
   `REJECTED` — §B.1.) Note `AUTO_APPLIED` already counts as resolved (the correct stage applied it);
   only `PENDING` blocks.
3. **Every referenced speaker has a usable voice.** `unresolved_voices(project) == []` — i.e. every
   speaker referenced by a renderable segment has an assigned `voice_clip_id` pointing at an existing
   `VoiceClip` whose `source_path` file is present on disk (narrator included). This is the
   **synthesize precondition surfaced here at the gate** instead of as a later synthesize FAILURE.

> **DECISION (recommend YES — gate enforces voice completeness):** Criterion 3 makes the review gate
> own voice-assignment completeness. Rationale: (a) the user is told *up front*, in the same review
> pass where they confirm attributions, that "narrator and Bob still need a voice," rather than
> sailing through review and hitting a synthesize FAILED later; (b) voice assignment *is* a review
> responsibility per CLAUDE.md ("nothing irreversible is automatic" — assigning a cast is a user
> decision), and it currently has no home; (c) it reuses `unresolved_voices` verbatim, so it is one
> tested predicate shared by both stages, not duplicated logic. The synthesize precheck **stays** as a
> defense-in-depth backstop (a voice file could vanish between review and synthesize), so this is
> belt-and-suspenders, not a move. See DECISIONS FOR USER #1.

### A.2 Signatures — `is_blocking` helper, `is_complete`, `run`

Put the **pure predicate logic** in a small Qt-free helper module so both `ReviewStage` and the UI
("what's still blocking?") share it, and the stage stays thin (mirrors how `attribution/policy.py`
holds the attribute stage's pure logic).

New module `src/casttrophizer/review/gate.py`:

```python
# src/casttrophizer/review/gate.py
from __future__ import annotations
from dataclasses import dataclass, field

from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.models import Project

__all__ = ["ReviewBlockers", "review_blockers", "is_review_complete"]


@dataclass(frozen=True)
class ReviewBlockers:
    """The lists the review UI surfaces; empty everywhere == ready to synthesize."""
    needs_attribution: list[str] = field(default_factory=list)   # segment ids still NEEDS_REVIEW
    pending_suggestions: list[str] = field(default_factory=list)  # TextSuggestion ids still PENDING
    unassigned_voices: list[str] = field(default_factory=list)    # speaker display names w/o voice

    @property
    def is_empty(self) -> bool:
        return not (self.needs_attribution or self.pending_suggestions or self.unassigned_voices)


def review_blockers(project: Project) -> ReviewBlockers:
    """Compute everything still blocking review, from persisted state (pure, offline)."""
    needs_attr = [
        seg.id
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == ReviewStatus.NEEDS_REVIEW
    ]
    pending = [
        sug.id
        for ch in project.book.chapters
        for ln in ch.lines
        for sug in ln.suggestions
        if sug.status == ReviewStatus.PENDING
    ]
    voices = unresolved_voices(project)  # reuse synthesize precondition verbatim
    return ReviewBlockers(
        needs_attribution=needs_attr,
        pending_suggestions=pending,
        unassigned_voices=voices,
    )


def is_review_complete(project: Project) -> bool:
    """True iff no attribution, suggestion, or voice blockers remain."""
    return review_blockers(project).is_empty
```

`src/casttrophizer/pipeline/stages/review.py` (fill the stub):

```python
class ReviewStage(Stage):
    name = StageName.REVIEW

    def is_complete(self, project: Project) -> bool:
        # Status-driven like every other stage; the gate writes COMPLETED in run().
        return project.stage_status.get(str(StageName.REVIEW)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        blockers = review_blockers(project)
        if not blockers.is_empty:
            # Persist nothing new; the runner halts and the UI works through `blockers`.
            return StageResult(self.name, ReviewStatus.NEEDS_REVIEW, _describe(blockers))
        project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(self.name, ReviewStatus.COMPLETED, "review complete")
```

`_describe(blockers)` renders a one-line human summary, e.g.
`"3 attributions, 1 text suggestion, voices needed for: narrator, Bob"` for the result `message` (the
UI / logs surface it). Keep it in `review/gate.py` or inline — small.

### A.3 The `is_complete` vs. content subtlety (important correctness note)

`is_complete` reads `stage_status[REVIEW] == COMPLETED`, consistent with every other stage and with
how `Pipeline.next_stage` resumes. But review's completeness is fundamentally **content-derived** (no
human action writes the flag directly — they edit segments/suggestions/voices). The resolution:

- `run` is the **single writer** of `stage_status[REVIEW] = COMPLETED`, and it only writes it when
  `review_blockers(project).is_empty`. So the flag can never be `COMPLETED` while blockers remain
  *as of the last run* — the gate is the gate.
- **Edge: a user edits *after* review was marked COMPLETED** (e.g. they go back and flip a segment to
  `NEEDS_REVIEW`, or un-assign a voice). `is_complete` would still return `True` from the stale flag,
  letting synthesize proceed. Two mitigations, pick one (DECISION FOR USER #3):
  - **(a) Recommended — review actions reset the flag.** Every §B mutation that can *introduce* a
    blocker (`reject`→`NEEDS_REVIEW`, reassign that re-flags, un-assign a voice) calls a shared
    `_invalidate_review(project)` that does `project.stage_status.pop(str(StageName.REVIEW), None)`
    before persisting. Cheap, local, keeps `stage_status` honest. The synthesize precheck is the
    backstop regardless.
  - **(b) Make `ReviewStage.is_complete` content-derived** (`return is_review_complete(project)`),
    ignoring the flag. Simpler conceptually and *self-correcting* (no stale flag possible), but it
    **breaks the "purely status-driven `is_complete`" convention** and means `run` need not write the
    flag at all. It also means `next_stage` recomputes blockers on every call (cheap here).
  - **Recommendation: (a)** — preserve the status-driven convention the other four stages rely on, and
    have the mutating actions keep the flag honest. The synthesize precheck remains the final guard.

### A.4 What `run` does NOT do

`run` performs **no mutation** of book content and makes **no provider call**. It does not auto-resolve
anything (that would violate "nothing irreversible is automatic"). It only computes blockers and
either halts (`NEEDS_REVIEW`) or records the gate passed (`COMPLETED`). It is idempotent and
resumable for free: re-running with blockers still present returns `NEEDS_REVIEW` again; re-running
once clear returns `COMPLETED`.

---

## B. The review ACTIONS (Qt-free, unit-testable operations the UI calls)

All mutations live in a Qt-free module so they unit-test offline and the PySide6 UI is a thin caller.
New module `src/casttrophizer/review/actions.py`. Each function **mutates the in-memory `Project`**
and the **caller (UI) persists** via `store.save(project)` — OR each action takes the `store` and
persists itself. **Recommendation: actions are pure mutations on `Project` and return the changed
object/ids; a thin `ReviewService` wrapper owns persistence** (mirrors how stages keep
orchestration pure and the stage owns `ctx.store.save`). This keeps the actions trivially testable
without a store and lets the UI batch a save. See DECISION FOR USER #4 for "persist per-action vs.
batched".

Concretely, two layers:

- `review/actions.py` — **pure functions** over `Project` (and its sub-objects). No I/O, no Qt.
- `review/service.py` — **`ReviewService`** holding a `WorkspaceStore`, exposing the same operations
  but calling `self._store.save(project)` after each (or on an explicit `commit()`), and calling
  `_invalidate_review` where §A.3(a) requires. The UI talks only to `ReviewService`.

### B.1 Text-suggestion actions

```python
# review/actions.py
def accept_suggestion(line: Line, suggestion_id: SuggestionId) -> None:
    """Apply a PENDING suggestion: set line.text = suggestion.suggested, status -> APPROVED."""

def reject_suggestion(line: Line, suggestion_id: SuggestionId) -> None:
    """Leave line.text unchanged, status -> REJECTED."""

def edit_line_text(line: Line, new_text: str) -> None:
    """Directly set line.text to a user-typed value (free-form correction).

    Does NOT touch line.segments — see B.5 note: editing a line's text after attribution does
    not auto-resegment. The text shown in review is line.text; segment.text drives synthesis.
    """
```

> **Important content note (flag for the UI phase, not blocking now):** `Line.text` and the
> concatenation of its `Segment.text` can diverge once attribution has run (segments carry the
> attributable spans). Accepting a text suggestion / editing line text updates **`line.text`** but the
> **segments are what synthesize renders**. For v1 of these *operations*, define the contract narrowly:
> `accept_suggestion`/`edit_line_text` mutate `line.text` only; whether/how that propagates to
> `segment.text` (re-segment? edit the matching segment?) is a **UI-phase design question** flagged in
> Risks. The gate (§A) does not depend on text/segment consistency, so this does not block this
> increment. **DECISION FOR USER #5.**

### B.2 Attribution actions

```python
def approve_attribution(segment: Segment) -> None:
    """User confirms the proposed speaker: review_status -> APPROVED (speaker_id/role unchanged)."""

def reject_attribution(segment: Segment) -> None:
    """User rejects the proposal without yet choosing a replacement: review_status -> REJECTED.

    NOTE: a REJECTED segment is NOT a gate blocker (only NEEDS_REVIEW is). If REJECTED should
    block synthesis until reassigned, change A.1 criterion 1 to also reject REJECTED — see
    DECISION FOR USER #6. Recommend: REJECTED is allowed past the gate ONLY if it still has a
    valid speaker_id+voice; otherwise the voice precheck catches an unattributed/voice-less one.
    """

def set_segment_speaker(
    segment: Segment, project: Project, *, speaker_id: SpeakerId | None,
    role: SpeakerRole, approve: bool = True,
) -> None:
    """Override the attribution: point the segment at speaker_id (None == narrator-by-id via
    resolve), set role, and (default) mark review_status APPROVED since the human chose it.
    Validates speaker_id exists in project.speakers (raises ValueError if not)."""
```

`set_segment_speaker` is the "confirm/override" path; `approve_attribution` is the fast "the proposal
was right" path. Reassigning to a brand-new character is §B.3 (create speaker) followed by
`set_segment_speaker`.

### B.3 Reassign to a different / new speaker

```python
def create_character(project: Project, name: str) -> Speaker:
    """Register a new CHARACTER speaker (reusing case-insensitive existing match, mirroring
    attribution.policy.resolve_speaker). Returns the existing or newly-appended Speaker."""

def reassign_segment_to_new_speaker(
    segment: Segment, project: Project, name: str,
) -> Speaker:
    """create_character(name) then set_segment_speaker(segment, speaker_id=speaker.id,
    role=CHARACTER, approve=True). Convenience for the UI 'assign to new character' path."""
```

> Reuse `attribution.policy.resolve_speaker` / `ensure_narrator` rather than re-implementing
> case-folded lookup. `create_character` can delegate to `resolve_speaker(project, ensure_narrator(...),
> name)` for non-None names; keep one matching rule.

**Edge — speaker with zero remaining segments after reassignment.** When the user reassigns the last
segment that pointed at speaker X, X becomes "orphaned" (no segment references it). This is
**harmless**: `unresolved_voices` only reports speakers *referenced by a segment*, so an orphaned
speaker with no voice does **not** block the gate. Do **not** auto-delete orphaned speakers (a later
reassignment might reference them again, and deletion is the kind of irreversible action CLAUDE.md
says to avoid). Optionally expose `prune_orphan_speakers(project) -> list[Speaker]` as an explicit,
user-driven cleanup (out of scope for this increment — flag).

### B.4 Voice-clip registration & assignment (the missing piece synthesize needs)

This is the home for the voice assignment that currently exists nowhere.

```python
def register_voice_clip(project: Project, source_path: str | Path, label: str) -> VoiceClip:
    """Create a VoiceClip(id=new_id('voice'), source_path=<abs path>, label=...) from a
    user-picked file and append to project.voice_clips. The file is a READ-ONLY input —
    referenced by absolute path, never copied/mutated (CLAUDE.md). Stores str(Path(source_path)).
    Does NOT validate playable audio here (UI may pre-validate); MAY assert the path exists
    (recommend: assert exists, raise ValueError if not, so a typo'd path surfaces immediately)."""

def assign_voice(speaker: Speaker, voice_clip: VoiceClip) -> None:
    """Map a speaker (narrator OR character — identical handling) to a registered clip:
    speaker.voice_clip_id = voice_clip.id."""

def assign_voice_by_ids(project: Project, *, speaker_id: SpeakerId, voice_clip_id: VoiceClipId) -> None:
    """Lookup-and-assign convenience; raises ValueError if either id is absent."""

def unassign_voice(speaker: Speaker) -> None:
    """Clear speaker.voice_clip_id (-> None). Re-introduces a voice blocker; the service must
    _invalidate_review (A.3a)."""
```

**Narrator is handled identically to a character** — it is just a `Speaker` (the attribute stage's
`ensure_narrator` already created it with `role=NARRATOR`, `voice_clip_id=None`). There is **no**
narrator-specific field and we add none (consistent with the synthesize plan's confirmed decision).
The UI offers "assign narrator voice" as the same `assign_voice` call against the narrator speaker.

**Edge — voice-clip file that disappears.** `register_voice_clip` records an absolute path; the file
is read-only and may later be moved/deleted. `unresolved_voices` already checks `source_path` file
existence on disk, so a vanished clip **re-opens the gate** (the speaker shows as unresolved again) and
the synthesize precheck catches it as a backstop. No extra handling needed beyond surfacing it.

### B.5 Consequence on the synthesize cache (NOTE only — do not build)

These actions change inputs that feed `AudioCache.key_for`:

- `edit_line_text` / a future segment-text edit → changes `segment.text` (if propagated) → new key.
- `set_segment_speaker` / `reassign_*` → changes the resolved `voice_clip_id` (different speaker →
  different clip) → new key.
- `assign_voice` / `unassign_voice` → changes a speaker's `voice_clip_id` → new key for that
  speaker's segments.

Each new key means synthesize **re-renders only the affected segments** on the next run; unaffected
segments stay cache-hits. **No review action calls the cache or deletes WAVs** — the key recomputation
in `synthesize_chapter` is the entire mechanism (orphaned old WAVs are harmless, GC deferred). This
plan only documents the consequence so the tester asserts it indirectly (a reassigned segment's
`AudioCache.key_for` differs before/after).

### B.6 `ReviewService` (the persistence-owning wrapper the UI uses)

```python
# review/service.py
class ReviewService:
    def __init__(self, store: WorkspaceStore, project: Project) -> None: ...
    # one method per B.1–B.4 action, each: mutate via review.actions.*, then
    #   if the action can introduce a blocker -> _invalidate_review(self._project)
    #   self._store.save(self._project)
    def blockers(self) -> ReviewBlockers: ...   # delegates to review.gate.review_blockers
```

The UI constructs one `ReviewService` per loaded project and calls it from the main thread (these are
cheap, synchronous mutations + a small JSON write — they do **not** need a worker thread, unlike
parse/attribute/synthesize). The threading rule (long work off the Qt main thread) is satisfied
trivially: review actions are not long-running. Running the *pipeline* (`Pipeline.run` hitting
`ReviewStage`) still goes through `PipelineWorker` like the other stages.

---

## C. UI scope — recommendation (flag for your decision)

**Recommended scope for THIS increment (headless, no GUI):**

1. `review/gate.py` — `ReviewBlockers`, `review_blockers`, `is_review_complete`, `_describe`.
2. `review/actions.py` — the pure mutation functions (§B.1–B.4).
3. `review/service.py` — `ReviewService` (persistence + `_invalidate_review`).
4. `pipeline/stages/review.py` — fill `ReviewStage.is_complete` / `run`.
5. Full offline test suite (§Test plan) + fixtures.

**Deferred to a separate follow-on phase (the first real GUI work):**

- The PySide6 review panels: per-line list, text-edit field, attribution confirm/override controls,
  speaker picker / "new character," voice-clip file picker + per-speaker assignment table, and a
  "blockers remaining" summary driven by `ReviewBlockers`.
- The QThread wiring to run `Pipeline` (through `ReviewStage` and onward) off the main thread via the
  existing `PipelineWorker` / `QtProgressReporter` (note: review *actions* are synchronous and don't
  need the worker; only the *pipeline run* does, and that machinery already exists).
- **Per-line AUDIO review** (goal #3): auditioning generated clips, approve/regenerate. This is
  **post-synthesize** (it needs rendered WAVs and operates on `Segment.audio_status`, not
  `review_status`), so it is a logically *later* review surface than the pre-synthesis review this plan
  defines. Keep it entirely separate. The synthesize plan already reserves `audio_status` transitions
  (`COMPLETED` rendered → user `APPROVED` / reset to `PENDING` to regenerate) for that phase.

> **Why split here:** building the four headless pieces makes the pipeline *correct and resumable*
> (review actually gates), satisfies *synthesize's voice precondition* (voices now have a home to be
> assigned), and is *fully testable offline* — all without committing to the large, first-of-its-kind
> GUI surface. The GUI can then be built against a stable, tested operation layer. **DECISION FOR USER
> #2.**

> **GSD note:** the deferred PySide6 review UI (per-line list + text edit + attribution controls +
> voice assignment table + later audio audition) is a large, multi-surface feature and the project's
> first real GUI. When you pick it up, consider `/gsd-plan-phase` (or `/gsd-ui-phase` for a UI design
> contract) rather than a single inline plan — it warrants the heavier workflow. This headless
> increment does not.

---

## Schema impact — NONE required

Every review action writes only fields that already exist and round-trip:

| Action writes | Field | Already serialized? |
|---|---|---|
| accept/reject/edit text | `Line.text`, `TextSuggestion.status` | yes (`_line_to_dict`, `_suggestion_to_dict`) |
| approve/reject/override attribution | `Segment.review_status`, `.speaker_id`, `.role` | yes (`_segment_to_dict`) |
| create/reassign speaker | `project.speakers[*]` (`name`,`role`,`voice_clip_id`) | yes (`_speaker_to_dict`) |
| register voice clip | `project.voice_clips[*]` (`id`,`source_path`,`label`) | yes (`_voice_clip_to_dict`) |
| assign/unassign voice | `Speaker.voice_clip_id` | yes |
| gate completion | `stage_status[REVIEW]` | yes (`stage_status` dict) |

`CURRENT_SCHEMA_VERSION` stays **1**. No migration. The only thing that *would* force a bump is an
out-of-scope feature (e.g. storing a per-segment LLM rationale, a project-level default narrator clip,
or recording review provenance/audit on each segment) — all **deferred**, flagged in Risks.

---

## Edge cases (operations + gate must handle; tester must cover)

| Case | Required behavior |
|---|---|
| Speaker with zero remaining segments after reassignment | Orphaned speaker is harmless; `unresolved_voices` ignores unreferenced speakers, so it does NOT block the gate. No auto-delete. |
| Rejecting **all** of a line's attributions | Each `reject_attribution` sets `REJECTED` (not `NEEDS_REVIEW`), so it clears those gate blockers; but if a rejected segment now has no valid speaker/voice, the voice precheck (criterion 3) re-blocks it. The UI flow is reject → reassign → it has a speaker again. (See DECISION #6 on whether bare `REJECTED` should itself block.) |
| Narrator voice assignment | `assign_voice(narrator_speaker, clip)` — identical to character; narrator is a `Speaker`. After this, `unresolved_voices` drops "narrator". |
| Voice-clip file disappears after registration | `unresolved_voices` checks `source_path` file existence → speaker re-shows as unresolved → gate re-opens; synthesize precheck is the backstop. No crash. |
| `register_voice_clip` with a non-existent path | Recommend: raise `ValueError` immediately (don't register a dead path). Confirm in DECISION #7. |
| Re-running `ReviewStage.run` with blockers present | Returns `NEEDS_REVIEW` again (idempotent halt); writes nothing. |
| Re-running once all blockers cleared | Sets `stage_status[REVIEW]=COMPLETED`, persists, returns COMPLETED; a further re-run is a no-op via `is_complete`. |
| User edits after COMPLETED (re-flags a segment / un-assigns a voice) | A.3(a): the mutating action `_invalidate_review` pops `stage_status[REVIEW]`, so `next_stage` returns review again; synthesize precheck is the backstop either way. |
| Empty book / all-narration (no NEEDS_REVIEW, no PENDING, all narrator with a voice) | `review_blockers` empty immediately → `run` returns COMPLETED on first call (no human action needed). Valid. |
| Attribute left segments APPROVED (high-confidence) + some NEEDS_REVIEW | Only the NEEDS_REVIEW ones block; APPROVED ones need no action. |
| `AUTO_APPLIED` text suggestions | Not blocking (only `PENDING` blocks); already-applied corrections are done. |

---

## Test plan (for the tester — fully offline/deterministic, no Qt, no real providers)

Add `tests/review/` (gate + actions unit tests) and `tests/pipeline/test_review_stage.py`
(integration). Add a `review_ready_project` fixture to `conftest.py`: a saved project with
`stage_status[PARSE/CORRECT/ATTRIBUTE]=COMPLETED` deliberately carrying **mixed blocker state**:

- a segment with `review_status=NEEDS_REVIEW` (attribution blocker);
- a line with a `TextSuggestion(status=PENDING)` (suggestion blocker);
- a referenced speaker (e.g. "Bob") with `voice_clip_id=None` (voice blocker);
- plus already-resolved items (an APPROVED segment, an AUTO_APPLIED suggestion, a voiced narrator/Alice)
  so "resolved doesn't block" is also asserted.

Reuse `fake_voice_clips` for `register_voice_clip`/`assign_voice` tests. No Qt import anywhere in these
tests (the actions/gate are Qt-free — that is the whole point).

### Gate tests (`tests/review/test_gate.py`)
- `review_blockers` on `review_ready_project` returns non-empty `needs_attribution`,
  `pending_suggestions`, **and** `unassigned_voices` (names "Bob"); `is_empty` is False.
- After resolving all three (via §B actions in the test), `is_empty` is True;
  `unassigned_voices == []` matches `unresolved_voices` exactly (assert they agree).
- `is_review_complete` False then True across the same transition.
- An already-clean project (`sample_project` has only one NEEDS_REVIEW segment + voices assigned):
  resolve that one segment → complete.

### Actions tests (`tests/review/test_actions.py`) — pure, no store
- `accept_suggestion`: `line.text == suggested`, `status == APPROVED`.
- `reject_suggestion`: `line.text` unchanged, `status == REJECTED`.
- `edit_line_text`: `line.text == new_text`; `line.segments` untouched.
- `approve_attribution`: `review_status == APPROVED`; `speaker_id`/`role` unchanged.
- `reject_attribution`: `review_status == REJECTED`.
- `set_segment_speaker`: points at the given speaker, sets role, APPROVED; raises `ValueError` for an
  unknown `speaker_id`.
- `create_character`: new name appends a `CHARACTER` Speaker; existing (case-insensitive) name reuses
  it (no dup) — assert it agrees with `resolve_speaker`.
- `reassign_segment_to_new_speaker`: creates speaker + repoints segment + APPROVED in one call.
- `register_voice_clip`: appends a `VoiceClip` with the absolute `source_path` and label; the source
  file is NOT copied (assert no file written under the workspace `audio/`); raises `ValueError` on a
  missing path (per DECISION #7).
- `assign_voice` / `assign_voice_by_ids`: sets `speaker.voice_clip_id`; satisfies the synthesize
  precheck (`unresolved_voices` drops that speaker). `unassign_voice` re-introduces the blocker.
- **Cache-consequence (indirect):** capture `AudioCache.key_for(segment, project)` before/after a
  `set_segment_speaker` that changes the resolved voice, and before/after an `assign_voice` — assert
  the key **changes** (proves the §B.5 re-render-on-edit consequence) without invoking TTS.

### Service tests (`tests/review/test_service.py`)
- Each `ReviewService` method mutates and **persists** (assert by `store.load()` round-trip).
- A blocker-introducing action (`reject_attribution`→NEEDS_REVIEW? no — REJECTED; use `unassign_voice`)
  pops `stage_status[REVIEW]` when it was COMPLETED (A.3a), proven by reload.
- Reload round-trips every mutated field (no schema bump, `CURRENT_SCHEMA_VERSION == 1`).

### Stage integration tests (`tests/pipeline/test_review_stage.py`) — against the reloaded project
- **Blockers present ⇒ NEEDS_REVIEW.** `ReviewStage.run(review_ready_project)` returns
  `StageResult(stage=REVIEW, status=NEEDS_REVIEW)`; `stage_status[REVIEW]` stays unset; `message`
  mentions the blockers.
- **In a full `Pipeline([... Attribute, Review, Synthesize])`, the runner HALTS at review:**
  `pipeline.run(ctx)` returns the review `NEEDS_REVIEW` result and synthesize never runs (assert no TTS
  calls / synthesize status unset). Proves the gate integrates with `_HALTING`.
- **Clear all blockers (via `ReviewService`), re-run ⇒ COMPLETED:** `stage_status[REVIEW]=COMPLETED`,
  persisted (reload proves it); `is_complete` True; `next_stage` advances to `synthesize`.
- **Voice-precondition parity:** after review COMPLETED, `unresolved_voices(project) == []`, so the
  downstream synthesize precheck passes (run synthesize with `FakeTTSProvider` and assert it does NOT
  FAIL on voices). This is the cross-stage proof that review satisfies synthesize's precondition.
- **Idempotency:** run twice with blockers → NEEDS_REVIEW both times, nothing written; run twice once
  clear → COMPLETED then a no-op (`is_complete` short-circuits in the pipeline).
- **Edit-after-complete (A.3a):** mark complete, `unassign_voice` via service → `is_complete` False
  again (flag popped), `next_stage` returns review.
- **No providers needed:** `StageContext` with `llm=None, tts=None` runs the review stage fine
  (assert it doesn't touch `ctx.llm`/`ctx.tts`).
- **Lazy-import / Qt-free:** extend `tests/test_lazy_imports.py` so importing `casttrophizer.review.*`
  and `casttrophizer.pipeline.stages.review` loads **no** `PySide6` (and no `anthropic`/`chatterbox`).
- **Tooling:** `ruff` / `black --check` / `mypy src` / `pytest` green.

### `conftest.py` additions
- `review_ready_project` (above). Keep it minimal and exact so blocker counts are assertable.
- (Optional) a small `review_service` fixture wrapping `ReviewService(tmp_workspace, project)`.

---

## Ordered task list (coder)

Dependency order: 0 (decisions) → 1 (gate) → 2 (actions) → 3 (service) → 4 (stage uses 1+3) →
5 (fixtures/tests) → 6 (tooling).

0. **Get user sign-off** on the DECISIONS FOR USER list below (esp. #1 gate-enforces-voices, #2
   headless-now scope, #3 stale-flag mitigation, #6 REJECTED-blocks?).
1. **Gate** — `src/casttrophizer/review/gate.py`: `ReviewBlockers`, `review_blockers`,
   `is_review_complete`, `_describe`. Reuse `audio.synthesize.unresolved_voices` (do not reimplement).
   `src/casttrophizer/review/__init__.py` re-exporting the public names, Qt-free.
2. **Actions** — `src/casttrophizer/review/actions.py`: §B.1–B.4 pure functions. Reuse
   `attribution.policy.resolve_speaker`/`ensure_narrator` for speaker creation/lookup.
3. **Service** — `src/casttrophizer/review/service.py`: `ReviewService` (persist per action, plus
   `_invalidate_review` on blocker-introducing actions per A.3a) + `blockers()`.
4. **ReviewStage** — fill `is_complete` (status-driven) + `run` (compute `review_blockers`;
   NEEDS_REVIEW if any, else write `stage_status[REVIEW]=COMPLETED` + save + COMPLETED). Qt-free; no
   provider use.
5. **Fixtures + tests** — `review_ready_project` in `conftest.py`; `tests/review/test_gate.py`,
   `test_actions.py`, `test_service.py`; `tests/pipeline/test_review_stage.py` (§Test plan).
6. **Tooling** — `ruff check`, `black --check`, `mypy src`, `pytest` green; extend
   `tests/test_lazy_imports.py` so `review.*` and `pipeline.stages.review` pull in no PySide6/SDKs.

### Module layout

| Path | Purpose |
|---|---|
| `src/casttrophizer/review/__init__.py` | re-exports (`review_blockers`, `is_review_complete`, `ReviewBlockers`, action fns, `ReviewService`); Qt-free, cheap import. |
| `src/casttrophizer/review/gate.py` | gate predicate + `ReviewBlockers` (§A). |
| `src/casttrophizer/review/actions.py` | pure mutation functions (§B.1–B.4). |
| `src/casttrophizer/review/service.py` | `ReviewService` — persistence + flag invalidation (§B.6, A.3a). |
| `src/casttrophizer/pipeline/stages/review.py` | fill `ReviewStage.is_complete`/`run` (§A.2). |

---

## Risks & open questions

- **`Line.text` vs `Segment.text` divergence after attribution (§B.5 note).** Accepting a text
  suggestion / editing line text updates `line.text`, but synthesize renders `segment.text`. How an
  edit propagates to segments (re-segment the line? edit the spanning segment? leave it?) is a real
  design question deferred to the UI phase. The gate does not depend on consistency, so it does not
  block this increment — but flag loudly: a user "fixing" a typo in review may not change what's
  spoken unless propagation is defined. **DECISION FOR USER #5.**
- **Does bare `REJECTED` (no reassignment) pass the gate?** A.1 criterion 1 only blocks on
  `NEEDS_REVIEW`. A segment the user `REJECTED` but never reassigned still has its old `speaker_id`;
  the voice precheck catches it only if that speaker lacks a voice. If a `REJECTED`-without-reassign
  should always block, add it to criterion 1. **DECISION FOR USER #6** (recommend: don't add it —
  rely on the voice precheck + a UI that forces reject→reassign).
- **Stale `stage_status[REVIEW]` after later edits (§A.3).** Recommend mitigation (a) (actions
  invalidate the flag). The synthesize precheck is the backstop regardless. **DECISION FOR USER #3.**
- **Per-action vs batched persistence (§B.6).** Saving `project.json` after every keystroke-level
  action is fine for a single-user desktop app (atomic write, small file), but a batched "commit on
  panel close" is also reasonable. **DECISION FOR USER #4** (recommend: persist per action — simplest,
  crash-safe, and the file is small).
- **Orphan speakers / orphan cache WAVs** never GC'd — harmless, disk-only, deferred (flagged).
- **Audio review is a separate, later surface** (post-synthesize, `audio_status`) — explicitly NOT in
  this plan (§C).
- **Scope-creep guard:** do NOT build any PySide6 widget, the audio-audition review, speaker-merge
  (alias) resolution, segment re-segmentation on edit, orphan GC, or any schema field here. This
  increment stops at: a Qt-free gate that halts/passes the pipeline, Qt-free review-action operations
  (incl. voice registration/assignment) that mutate+persist the project, and their offline tests.

---

## Verification strategy (what the tester must prove)

1. **Gate correctness:** `review_blockers` reports exactly the NEEDS_REVIEW segments, PENDING
   suggestions, and unresolved-voice speakers; `is_empty`/`is_review_complete` flip only when all
   three are cleared. `unassigned_voices` agrees with `unresolved_voices` bit-for-bit.
2. **Stage gating:** `run` returns NEEDS_REVIEW while blockers remain (writes nothing), COMPLETED once
   clear (writes `stage_status[REVIEW]`, persists); the `Pipeline` runner HALTS at review and resumes
   to synthesize after.
3. **Each action mutates + persists correctly** (proven by `store.load()` round-trip): text
   accept/reject/edit, attribution approve/reject/override, speaker create/reassign, voice
   register/assign/unassign — every field round-trips, no schema bump.
4. **Synthesize precondition satisfied:** after review COMPLETED, `unresolved_voices == []` and the
   synthesize stage does not FAIL on voices (run it with `FakeTTSProvider`).
5. **Read-only inputs honored:** `register_voice_clip` references the source path, copies nothing into
   the workspace.
6. **Cache consequence (indirect):** a reassignment / voice change makes `AudioCache.key_for` differ
   (so synthesize will re-render only that segment) — asserted without any TTS call.
7. **Edge cases** (orphan speaker, reject-all, narrator voice, vanished clip, edit-after-complete) per
   the table.
8. **Qt-free & offline:** review modules import no PySide6/anthropic/chatterbox; tests use fakes only.
9. `ruff` / `black` / `mypy src` / `pytest` green.

---

## Handoff

**Coder, once decisions land, build Task 1 first:** `src/casttrophizer/review/gate.py` —
`ReviewBlockers` + `review_blockers(project)` + `is_review_complete(project)`, computed purely from
persisted state and **reusing `casttrophizer.audio.synthesize.unresolved_voices` verbatim** for the
voice criterion. It is the seam both `ReviewStage` (Task 4) and the future UI ("what's blocking?")
depend on, and it is fully testable offline with no Qt and no providers. Then the pure `actions.py`
(Task 2), the persisting `ReviewService` (Task 3), and finally wire `ReviewStage.run`/`is_complete`
(Task 4) to halt the pipeline on `NEEDS_REVIEW` and record `COMPLETED` when the gate is clear. The
PySide6 review UI and post-synthesize audio review are a **separate phase** (§C) — do not start them
here.

---

## DECISIONS FOR USER — CONFIRMED (build to these)

1. **Review gate enforces voice-assignment completeness: CONFIRMED — YES.** Gate criterion 3 =
   `unresolved_voices(project) == []` (reuse the synthesize precheck verbatim), so review surfaces up
   front which speakers — including the narrator — still need a voice. Synthesize keeps its precheck as
   a backstop.
2. **Scope of THIS increment: CONFIRMED — headless gate + review-action operations (incl. voice
   registration/assignment) + offline tests now.** The full PySide6 review UI + QThread wiring is a
   SEPARATE follow-on phase; per-line AUDIO review (post-synthesize, `audio_status`) is a later distinct
   surface. No GUI code in this increment.
3. **Text-edit propagation (orig §5): CONFIRMED — DEFER to the UI phase.** `accept_suggestion`/
   `edit_line_text` mutate `line.text` only; `segments` (what synthesize speaks) are left untouched.
   Re-segmentation/propagation on edit is a UI-phase design decision. (Known limitation: a text "fix"
   in review won't change spoken audio yet — most text fixes belong in the correct stage anyway.)
   Accepted minor defaults: per-action persistence; blocker-introducing actions pop
   `stage_status[REVIEW]`; a bare REJECTED attribution does NOT block the gate (only NEEDS_REVIEW does);
   `register_voice_clip` raises `ValueError` on a non-existent source path.
3. **Stale `stage_status[REVIEW]` after a later edit (§A.3).**
   **Recommend (a):** blocker-introducing review actions call `_invalidate_review` (pop the flag);
   keep `is_complete` status-driven like the other stages; synthesize precheck is the backstop.
   (Alternative (b): make `ReviewStage.is_complete` content-derived — simpler but breaks the
   status-driven convention.)
4. **Persistence granularity (§B.6).**
   **Recommend: persist per action** (`ReviewService` saves after each mutation) — crash-safe, simple,
   and `project.json` is small with atomic writes. (Alternative: batch + explicit `commit()`.)
5. **`Line.text` vs `Segment.text` propagation on text edits (§B.5 note).**
   **Recommend: defer to the UI phase.** For now, `accept_suggestion`/`edit_line_text` mutate
   `line.text` only and leave `segments` untouched; how/whether a text edit re-segments or edits the
   spanning segment (which is what synthesize actually speaks) is a UI-phase design decision. Flag that
   a user "fix" in review may not change spoken audio until propagation is defined.
6. **Should a bare `REJECTED` attribution (no reassignment yet) block the gate?**
   **Recommend NO** — only `NEEDS_REVIEW` blocks (criterion 1); the voice precheck catches a rejected
   segment that ends up with no valid speaker/voice, and the intended UI flow is reject→reassign. (If
   you want REJECTED to hard-block until reassigned, add it to criterion 1.)
7. **`register_voice_clip` with a non-existent source path.**
   **Recommend: raise `ValueError` immediately** (don't register a dead path), so a typo surfaces at
   assignment time rather than as a synthesize precheck failure much later.
