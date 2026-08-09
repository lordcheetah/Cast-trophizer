# Plan: Speaker Attribution Stage (`SegmentAttributeStage`)

**Goal:** Turn every parsed `Line` (currently `segments == []`) into one or more attributed
`Segment`s — narration vs. inline dialogue — where each segment is tagged narrator or a named
speaker with a confidence, low-confidence attributions are flagged `NEEDS_REVIEW` (never silently
committed), discovered character names are registered in `project.speakers`, and the stage is
idempotent/resumable. This is the first stage that drives `ctx.llm`.

Pipeline stage: **`attribute`** (third stage; after `correct`, before `review`). It reads parsed
+ corrected `Line.text`, **deterministically segments each line offline**, calls the LLM **only**
to attribute speakers to those segments, writes `Line.segments`, grows `project.speakers`,
persists via the store, and sets `stage_status[ATTRIBUTE] = COMPLETED`.

---

## 1. Decisions locked by the scaffold (do not relitigate)

- `Segment(id, text, speaker_id, role, confidence, review_status, audio_cache_key=None,
  audio_status=PENDING)` already exists and serializes (`_segment_to_dict`/`_from_dict`). Use it
  as-is. `speaker_id is None` ⇔ narrator (mirrors `AttributionCandidate.speaker_name is None`).
- `LLMProvider.attribute_speakers(*, context: str, candidates: list[str],
  known_speakers: list[str]) -> list[AttributionCandidate]` is the **only** attribution entry
  point. `AttributionCandidate(segment_id, speaker_name, confidence, rationale)`. `candidates`
  are **segment ids** (strings); the LLM returns one candidate per id with a `speaker_name`
  (`None` ⇒ narrator).
- `SpeakerRole` is exactly `{NARRATOR, CHARACTER}`. `Speaker(id, name, role, voice_clip_id=None)`
  — a discovered character gets `role=CHARACTER`, `voice_clip_id=None` (no clip yet).
- `ReviewStatus` already has `NEEDS_REVIEW`, `APPROVED`, `PENDING`, `COMPLETED`, `STOPPED`,
  `FAILED`. Segments use `review_status` (attribution review) distinct from `audio_status`.
- Stage contract (`pipeline/stage.py`): `run` polls `ctx.progress.should_stop()` at safe
  checkpoints, returns `STOPPED` with partial state persisted, is idempotent/resumable, never
  imports Qt. `is_complete` is purely `stage_status`-driven. `stage_status` keys are
  `str(StageName.X)`.
- The runner halts on `STOPPED / NEEDS_REVIEW / FAILED`; `COMPLETED` proceeds to `review`.
- `StageContext` carries `llm: LLMProvider | None` and `config: AppConfig | None`. The stage
  receives an already-built provider via `ctx.llm` — **stages never call the factory** (the runner
  / UI wires it). Mirror `ParseStage(parser=...)` / `CorrectTextStage(correctors=...)`: allow a
  constructor override (`SegmentAttributeStage(segmenter=...)`) for test injection of the
  segmenter, but the **LLM always comes from `ctx.llm`** (so tests pass `FakeLLMProvider`).
- Serialization round-trips `Line.segments` and `project.speakers` already; **no schema change is
  required** (§8 confirms).

---

## 2. Where this sits & what it reads/writes

| | |
|---|---|
| **Reads** | `project.book.chapters[*].lines[*].text` (parsed + corrected); `project.speakers` (seed `known_speakers`). |
| **Writes** | `Line.segments` (one or more attributed `Segment`s per line); `project.speakers` (new `CHARACTER`s + a reserved narrator). `Line.text` / `Line.suggestions` untouched. |
| **Uses** | `ctx.llm.attribute_speakers(...)` — the only LLM call. Segmentation is offline. |
| **Persists** | whole `Project` via `ctx.store.save`. |
| **Records** | `stage_status[str(StageName.ATTRIBUTE)] = COMPLETED`. |
| **Progress** | per **chapter** (`set_total(n_chapters)`, one `advance` each) — matches parse/correct; the LLM-request unit is the **batch within a chapter** (§5). |

---

## 3. Segmentation (Line → Segments) — RECOMMENDED: deterministic offline splitter

**Recommendation: segmentation is deterministic code, NOT the LLM.** A new offline module
`src/casttrophizer/attribution/segmenter.py` (mirrors how `text/` and `ebook/` isolate a concern),
behind a tiny interface so it stays swappable and unit-testable without a model.

Why deterministic, not LLM-driven:
- The segment is the **per-segment audio cache key unit** (`Segment.audio_cache_key`). It must be
  **stable and reproducible** so re-running attribution on an unchanged line yields the same
  segment boundaries (else cached audio is needlessly invalidated downstream). An LLM splitter is
  non-deterministic and would churn cache keys.
- It keeps the LLM job narrow ("who speaks this span?"), which is where even strong models add
  value and weak models (LM Studio fallback) stay usable.
- It's fully offline/testable in CI with no mocking of split logic.

### 3a. Interface & DTO

```python
# src/casttrophizer/attribution/segmenter.py
@dataclass(frozen=True)
class SegmentSpan:
    text: str                 # the span's text (trimmed, non-empty)
    kind: str                 # "narration" | "quote"

class Segmenter(Protocol):
    def split(self, line_text: str) -> list[SegmentSpan]:
        """Split one line into ordered narration/quote spans. Pure, deterministic, offline.
        A line with no recognizable quote returns a single narration span == the whole line."""
        ...
```

Concrete `QuoteSegmenter` implements the rules below. The stage takes
`SegmentAttributeStage(segmenter: Segmenter | None = None)`; default is `QuoteSegmenter()`.

### 3b. Quote/dialogue rules (v1 — FLAG conventions to support)

Recommended v1 ruleset (deterministic, regex/scan-based):

1. **Straight and curly double quotes are quote delimiters:** `"…"` and `“…”`. A quote span is the
   text **including** its delimiters (so the rendered segment reads the quote naturally; punctuation
   inside is preserved). Curly open/close (`“`/`”`) are matched as a pair; straight `"` toggles.
2. **Narration between/around quotes** becomes its own narration span, including dialogue tags
   (`said Alice`, `she replied`) — the tag stays narration (narrator voice), the quote is the
   character. Example: `The narrator spoke. "Hello," said Alice.` →
   `[narration "The narrator spoke. "], [quote '"Hello,"'], [narration " said Alice."]`
   (whitespace trimmed per-span; empty spans dropped).
3. **No quotes ⇒ single narration span == whole line** (headings, pure narration, empty-after-trim
   lines produce **zero** segments — see §9).
4. **Mismatched / unbalanced quotes** (odd count of straight `"`, or an open curly with no close
   within the line): treat the **whole line as one narration span** (do not guess a boundary).
   Flag a debug log; do not fail. This is the safe degenerate.
5. **Multi-paragraph quotes** (a quote opened in one `Line` and closed in a later `Line`): **NOT
   handled in v1** — segmentation is per-line, so an unclosed quote in a line falls to rule 4
   (whole line narration) for that line. FLAG as a known limitation (real but lower-frequency;
   needs cross-line state, deferred).
6. **Em-dash dialogue** (`— Hello, said the man.`, common in European typography): **NOT handled
   in v1** — em-dash lines become a single narration span. FLAG as a known limitation; add as a
   second `Segmenter` rule later without touching the stage.
7. **Single quotes / apostrophes are NOT quote delimiters in v1** (`'` is overwhelmingly a
   contraction/possessive in English prose; treating it as dialogue would mis-split `don't`,
   `Alice's`). Nested single-inside-double quotes therefore stay inside their double-quote span.
   FLAG if the target books use single-quote dialogue (British convention) — then we need a
   configurable primary-quote char.
8. **Quotes spanning sentence boundaries within one line** are fine — a quote span can contain
   `. ! ?`; we split on **quote delimiters, not sentences.**

> The cache-key stability contract: given identical `line.text`, `QuoteSegmenter.split` must return
> identical spans (same text, same order). The tester pins this.

### 3c. Mapping spans → `Segment`s (pre-attribution)

For each `SegmentSpan` the stage creates a `Segment` with a **fresh id** (`new_id("seg")`),
`text=span.text`, `role=NARRATOR` and `speaker_id=<narrator id>` as the **provisional default**,
`confidence=0.0`, `review_status=NEEDS_REVIEW`, `audio_cache_key=None`,
`audio_status=PENDING`. Narration spans are then **not sent to the LLM** (narration is the
narrator by construction — confidence 1.0, `review_status=APPROVED`); **quote spans** are the
`candidates` sent to `attribute_speakers` (§4). This minimizes LLM tokens (we never ask "who
narrates the narration?").

---

## 4. LLM attribution contract — uses the EXISTING signature unchanged

**No change to `providers/base.py` is required.** The existing
`attribute_speakers(*, context, candidates, known_speakers)` is sufficient:

- **`candidates: list[str]`** = the **segment ids of the quote spans** in the current batch (§5).
  The stage builds a `{segment_id -> Segment}` map for the batch so returned candidates map back
  by id.
- **`context: str`** = a rendered, deterministic prompt body for the batch: the surrounding lines
  of the chapter (a window — §5) with each quote span explicitly marked by its segment id, plus the
  narration around it, so the model has dialogue-tag and adjacency cues. Recommended rendering:
  number each line; inline-tag quote spans as `[SEG <id>] "…"`. Example context the **stage**
  builds (the provider just forwards it):
  ```
  Attribute each marked quote to its speaker. Lines:
  L1: The narrator set the scene.
  L2: [SEG seg_ab12] "Hello there," said Alice.
  L3: [SEG seg_cd34] "And hello to you," Bob replied.
  Speakers already known: narrator, Alice.
  Return one entry per SEG id.
  ```
- **`known_speakers: list[str]`** = the display names of `project.speakers` with
  `role==CHARACTER` (narrator is implicit). Lets the model reuse an existing name rather than
  inventing a variant.
- **Returns `list[AttributionCandidate]`** — one per `segment_id` in `candidates`. The stage
  resolves each:
  - `speaker_name is None` ⇒ narrator (`speaker_id = narrator.id`, `role=NARRATOR`).
  - `speaker_name` matches (case-folded) an existing `Speaker.name` ⇒ reuse its id.
  - else ⇒ **create** a new `Speaker(id=new_id("spk"), name=speaker_name, role=CHARACTER)` and
    append to `project.speakers` (§5 — the registry).
  - The returned `confidence` and the §6 policy set `Segment.confidence` and `review_status`.

### 4a. Prompt/response JSON shape (the provider impl's job, §7)

The **stage** does not parse JSON — it consumes typed `AttributionCandidate`s. The provider's
`attribute_speakers` impl is responsible for prompting the model to return strict JSON and parsing
it into `AttributionCandidate`s. Recommended response schema the provider asks the model for:
```json
{"attributions": [{"segment_id": "seg_ab12", "speaker": "Alice", "confidence": 0.9,
                   "rationale": "dialogue tag 'said Alice'"}]}
```
`speaker: null` (or `"narrator"`) ⇒ narrator. The provider:
- must return **exactly one** `AttributionCandidate` per requested id; for any **missing** id it
  returns a `0.0`-confidence narrator candidate (the stage then flags it `NEEDS_REVIEW` — §6/§9);
- must **drop/ignore** any `segment_id` the model returns that wasn't requested (defensive);
- on malformed/non-JSON output, see §7 (provider raises a typed error; stage flags the whole batch
  `NEEDS_REVIEW` and continues — never silently mis-attributes).

> **Defensive mapping in the stage** (don't trust the provider blindly): build the id→Segment map,
> iterate **requested ids**, look up the returned candidate by id; unknown returned ids are
> ignored, missing ones default to low-confidence-narrator → `NEEDS_REVIEW`. This makes the stage
> robust to both stub providers and weak local models.

**FLAG (provider interface):** none required. The current signature carries everything. The only
judgment call is *where the context string is built* — recommended in the **stage** (so the prompt
shape is provider-agnostic and testable with `FakeLLMProvider`, which records `context`), with the
provider wrapping it in its own system prompt + JSON instructions.

---

## 5. Speaker discovery, registry & narrator representation

### 5a. Narrator — reserved Speaker (recommended) AND `speaker_id` set

The model uses `speaker_id is None` ⇔ narrator at the *candidate* boundary, but on a persisted
`Segment` we recommend **pointing narrator segments at a reserved narrator `Speaker`** rather than
leaving `speaker_id=None`, because:
- the synthesize stage needs a `voice_clip_id` for the narrator too — a reserved `Speaker` is where
  the user assigns the narrator voice;
- `conftest.py`'s `sample_project` already models exactly this (a `narrator` Speaker with
  `role=NARRATOR`).

**Rule:** at the start of `run`, ensure a reserved narrator exists:
`_ensure_narrator(project) -> Speaker` finds the first `Speaker` with `role==NARRATOR` (case-fold
name `"narrator"`); if none, create `Speaker(new_id("spk"), name="narrator", role=NARRATOR)` and
append. Narrator segments get that `speaker_id` and `role=NARRATOR`. (Both representations are
internally consistent: `speaker_id` is non-None but the role is NARRATOR.)

### 5b. Character discovery

When the LLM returns a `speaker_name` not matching any existing speaker (case-folded compare over
`project.speakers[*].name`), create `Speaker(new_id("spk"), name=speaker_name, role=CHARACTER,
voice_clip_id=None)` and append to `project.speakers`. Re-use across batches/chapters via the same
case-folded lookup so "Alice" said twice yields one Speaker. This is the registry — it grows as
the book is processed; the user assigns voice clips later (review/voice-assignment UI).

### 5c. Aliases — FLAG (recommend DEFER)

Name variants ("Mr. Darcy" / "Darcy", "the Captain" / "Wentworth") are a real attribution problem
but **resolving them automatically is risky** (merging two characters silently is irreversible-ish
and exactly the kind of thing CLAUDE.md says to surface, not auto-commit). **Recommended for v1:
treat each distinct returned name as a distinct Speaker** (no auto-merge). Surface alias merging as
a **later, user-driven** review action (merge two speakers → reassign segments), out of scope for
this stage. The `known_speakers` list passed to the model is the soft mechanism that nudges it to
reuse an established name. FLAG: confirm defer-aliases vs. build a normalization pass now.

---

## 6. Confidence & review policy — FLAG threshold value

Per "nothing irreversible is automatic," attribution is **never auto-committed as final**; it is a
proposal the user confirms in the `review` stage. But we still set a sensible initial
`review_status` so the review UI can prioritize:

- **Narration segments** (not sent to LLM): `confidence = 1.0`, `review_status = APPROVED`
  (narrator-by-construction; user can still override in review).
- **Quote segments with returned `confidence >= THRESHOLD`:** `review_status = APPROVED`
  (high-confidence proposal; the user can still change it). `confidence` = returned value.
- **Quote segments with `confidence < THRESHOLD`** (and all defaulted/missing/malformed cases):
  `review_status = NEEDS_REVIEW`. These are what the runner/UI surfaces.

**Recommended `THRESHOLD = 0.75`** (a single threshold for v1; conservative — a weak local model's
guesses mostly land below it and get surfaced). Define it as a module constant
`ATTRIBUTION_CONFIDENCE_THRESHOLD = 0.75` in `attribution/policy.py` so it is one tested place,
mirroring how `text/base.py` centralizes `_PROPER_NOUN_MIN_COUNT`.

**Does the stage return `COMPLETED` or `NEEDS_REVIEW`?** Recommend **`COMPLETED`** (mirrors the
`correct` stage decision): the stage finishes the whole book and records COMPLETED even when many
segments are `NEEDS_REVIEW`; the dedicated `review` stage/UI is where the human resolves them. If
it returned `NEEDS_REVIEW`, the runner would halt the pipeline at attribution every run, which
conflates "stage done" with "human work pending." FLAG (minor): confirm COMPLETED.

> The float thresholding is the only policy gate; `review_status` (APPROVED vs NEEDS_REVIEW) is
> the user-facing signal, exactly paralleling `correct`'s AUTO_APPLIED-vs-PENDING split.

---

## 7. Provider availability / fallback & the stub providers

### 7a. Stage behavior

- `run` first **None-guards `ctx.llm`**: if `ctx.llm is None`, return
  `StageResult(ATTRIBUTE, FAILED, "no LLM provider configured")` and **do not** set
  `stage_status` (re-runnable once configured). Mirrors parse's FAILED-leaves-status-unset.
- Optionally check `ctx.llm.is_available()`; if False, FAILED with a clear message. (Recommended:
  yes — cheap, gives a good error instead of a deep SDK exception.)
- **Provider selection (Claude primary vs LM Studio backup) is NOT the stage's job.** The factory
  `build_llm_provider(config)` + `is_available()` fallback is the **caller's** responsibility (UI /
  a thin runner helper), consistent with "stages never construct providers." The stage just uses
  whatever `ctx.llm` it's handed. FLAG (minor): decide whether the primary→backup fallback lives in
  a small `providers` helper (`build_available_llm(config)`) now or is wired later in the UI. Recommend
  a tiny helper now so the stage has something to be wired to, but it's out of this stage's core.

### 7b. The stub `attribute_speakers` impls — FLAG: implement Claude now or ship against the fake?

`ClaudeProvider.attribute_speakers` / `LMStudioProvider.attribute_speakers` /`is_available` are
currently `raise NotImplementedError`. The stage **functions end-to-end against `FakeLLMProvider`**
(which already returns scripted `AttributionCandidate`s with a low-confidence first entry — perfect
for the review path). So the stage can land and be fully tested without any real provider.

**Recommended: ship the stage + segmenter against `FakeLLMProvider`, and implement the real
`ClaudeProvider.attribute_speakers` + `is_available` in the same PR** (it's small and is the
headline feature's actual payload — without it the stage can't run for a real user). Keep
`LMStudioProvider` as a documented TODO (same JSON contract, different client) unless the user wants
the local fallback now. The Claude impl is: build messages (system: "you attribute dialogue…
return strict JSON {attributions:[…]}"; user: the stage-built `context`), call
`messages.create(...)` with `temperature=0`, parse JSON, map to `AttributionCandidate`s, enforce
the one-per-requested-id + null⇒narrator rules, raise a typed `LLMProviderError` (add to
`errors.py` if absent) on non-JSON/HTTP failure. Consult the `claude-api` skill before writing the
SDK call; the model id comes from `config.claude_model` (already `claude-opus-4-8`).

**Alternative (smaller PR):** ship stage + fake only; leave BOTH providers stubbed with a TODO.
The stage is real and tested; a real run is blocked until a provider lands. Recommend the first
option (Claude now) since this is goal #1 and otherwise nothing actually attributes.

---

## 8. Schema impact — NONE required

`Segment` and `Speaker` already carry every field this stage writes, and `serialization.py`
round-trips `Line.segments` and `project.speakers`. **No `schema_version` bump, no migration.**
(Confirmed against `_segment_to_dict`/`_from_dict`, `_speaker_to_dict`/`_from_dict`.) Flag only if
the user wants a per-segment provenance field (e.g. store the LLM `rationale` on the Segment) — that
would be additive + a schema bump; **recommend deferring** (rationale is useful in the review UI but
can be regenerated/ignored for v1). The `AttributionCandidate.rationale` is currently dropped by the
stage; note as a possible future field.

---

## 9. Idempotency / resume (the critical correctness section)

`run` must be safe to call twice and after a user edits a line. The **idempotency key is
`Line.segments`**: a line that already has ≥1 segment has been attributed → **skip it** (no
re-segmentation, no LLM call). This mirrors `correct`'s "line already has a correction suggestion ⇒
skip" rule and `parse`'s "segments stay []" handoff.

Rules:
1. **Skip attributed lines.** In the per-chapter loop, `if line.segments: continue`. So a re-run
   creates **no duplicate segments** and makes **no extra LLM calls** (the tester asserts
   `FakeLLMProvider.attribute_calls` count is unchanged on the second run).
2. **Batch only unattributed lines.** A chapter's batch (§5b) is built from its **unattributed**
   lines only; a chapter fully attributed contributes zero LLM calls.
3. **Resume after STOP:** the stop checkpoint is **per chapter** (after persisting the chapter's
   newly-attributed lines). Already-attributed chapters are skipped on resume; the pass continues
   from the first chapter with unattributed lines. No double-attribution.
4. **Re-attributing after a user edits a line's text:** out of scope for the stage's default. The
   skip rule means an edited line is *not* automatically re-attributed. The intended mechanism is an
   explicit UI action "re-attribute this line" that **clears `line.segments` first** (which also
   invalidates that segment's audio cache downstream — see note), after which a re-run re-segments
   and re-attributes just that line. The stage's skip-on-nonempty rule makes this clean: clearing is
   the only re-trigger. FLAG (minor): confirm "edit ⇒ user must explicitly re-attribute" is the v1
   behavior (recommended; auto-detecting text drift needs a stored hash → schema bump).

> **Audio-cache interaction:** segments are the audio cache unit. Re-attributing a line replaces its
> `Segment`s (new ids) and thus orphans any previously-cached audio for the old segments — correct
> behavior (changed text/speaker must re-render), but the synthesize/cache stage must key on current
> segment identity. This stage doesn't touch audio; just don't reuse old segment ids when
> re-segmenting (always `new_id("seg")`).

### 9a. `is_complete`

```python
def is_complete(self, project: Project) -> bool:
    return project.stage_status.get(str(StageName.ATTRIBUTE)) == ReviewStatus.COMPLETED
```
Purely status-driven (like parse/correct). Do **not** also require every line to have segments — an
empty book or an all-empty-line chapter legitimately yields lines with zero segments (§9 edge).

---

## 10. Stage wiring (`src/casttrophizer/pipeline/stages/attribute.py`)

```python
def __init__(self, segmenter: Segmenter | None = None,
             threshold: float = ATTRIBUTION_CONFIDENCE_THRESHOLD) -> None:
    self._segmenter = segmenter or QuoteSegmenter()
    self._threshold = threshold

def run(self, project, ctx) -> StageResult:
    if ctx.llm is None:
        return StageResult(self.name, ReviewStatus.FAILED, "no LLM provider configured")
    if not ctx.llm.is_available():
        return StageResult(self.name, ReviewStatus.FAILED, f"LLM provider {ctx.llm.name} unavailable")

    narrator = ensure_narrator(project)        # §5a — reserved Speaker
    chapters = project.book.chapters
    ctx.progress.set_total(len(chapters))

    try:
        for chapter in chapters:
            if ctx.progress.should_stop():
                ctx.store.save(project)        # partial: completed chapters persisted
                return StageResult(self.name, ReviewStatus.STOPPED, "stopped during attribute")

            todo = [ln for ln in chapter.lines if not ln.segments]   # §9 skip attributed
            if todo:
                attribute_chapter(chapter, todo, narrator, project, ctx.llm,
                                  self._segmenter, self._threshold)   # §3-§6 (may batch, §5b)
            ctx.progress.advance(1, message=chapter.title)
    except LLMProviderError as exc:            # malformed/unreachable mid-run
        ctx.store.save(project)                # keep partial progress
        return StageResult(self.name, ReviewStatus.FAILED, str(exc))

    project.stage_status[str(StageName.ATTRIBUTE)] = ReviewStatus.COMPLETED
    ctx.store.save(project)
    return StageResult(self.name, ReviewStatus.COMPLETED, "speakers attributed")
```

`attribute_chapter` (in `attribution/attribute.py`, the policy/orchestration module so the stage
stays thin — mirrors `text/base.apply_fixes`):
1. For each `todo` line: `spans = segmenter.split(line.text)`; build `Segment`s (narration ⇒
   APPROVED narrator; quote ⇒ provisional NEEDS_REVIEW). Empty `spans` ⇒ line gets **zero**
   segments (heading/empty line — §9). Assign `line.segments = built`.
2. Collect all **quote** segments across the batch (chapter window, §5b); if none, no LLM call.
3. Build `context` (§4) and `candidates = [seg.id for quote segments]`,
   `known_speakers = [s.name for s in project.speakers if role==CHARACTER]`.
4. `results = llm.attribute_speakers(context=context, candidates=candidates,
   known_speakers=known_speakers)`; index by `segment_id`.
5. For each requested quote segment: resolve candidate (missing ⇒ default low-conf narrator),
   resolve/create Speaker (§5b), set `speaker_id/role/confidence/review_status` per §6 threshold.
6. Persist happens at the **stage** level per chapter (so `attribute_chapter` is pure-ish on the
   in-memory project; the stage calls `ctx.store.save` once per chapter — recommended: save after
   each chapter so STOP/resume is chapter-granular and a crash keeps finished chapters).

### Module layout

| Path | Purpose |
|---|---|
| `src/casttrophizer/attribution/__init__.py` | re-exports + `default_segmenter()`; keeps imports cheap/Qt-free. |
| `src/casttrophizer/attribution/segmenter.py` | `SegmentSpan`, `Segmenter` Protocol, `QuoteSegmenter` (§3). |
| `src/casttrophizer/attribution/policy.py` | `ATTRIBUTION_CONFIDENCE_THRESHOLD`, `review_status_for(confidence, threshold)`, `ensure_narrator`, `resolve_speaker`. |
| `src/casttrophizer/attribution/attribute.py` | `attribute_chapter(...)` orchestration + `build_context(...)`. |
| `src/casttrophizer/pipeline/stages/attribute.py` | fill `SegmentAttributeStage.run`/`is_complete` (§10). |
| `src/casttrophizer/providers/llm/claude.py` | implement `attribute_speakers` + `is_available` (§7b, if chosen). |
| `src/casttrophizer/errors.py` | add `LLMProviderError` if not present. |

---

## 11. Batching & cost control — FLAG batch granularity

The LLM request is **expensive** (cost + latency), so the unit matters. Options:

- per **quote span** (one call per dialogue line) — most calls, worst cost/latency, **least**
  context (model can't use adjacency). Reject.
- per **chapter** (one call carrying all the chapter's quote spans + full chapter text as context) —
  fewest calls, best adjacency context, but a long chapter can blow the token budget / output size.
- per **window of N lines** (a sliding window, e.g. 40 lines, with all quote spans inside marked) —
  bounded tokens, good local context.

**Recommended: per chapter, with a line-count cap that falls back to fixed-size windows.** Default
`MAX_LINES_PER_BATCH = 60`; a chapter with ≤60 lines is one call; a longer chapter is split into
consecutive windows of ≤60 lines (each carrying its own quote candidates + that window's text as
context). This keeps the common case to one call/chapter (cheap, max context) while bounding tokens
on huge chapters. Determinism for tests: windows are derived purely from line order, so
`FakeLLMProvider.attribute_calls` is reproducible; the tester can assert call count = number of
windows. FLAG: confirm `MAX_LINES_PER_BATCH = 60` (or "per chapter, no cap").

---

## 12. Edge cases (stage must handle; tester must cover)

| Case | Required behavior |
|---|---|
| Heading / pure-narration line (no quotes) | `split` ⇒ 1 narration span ⇒ 1 narrator Segment (APPROVED). |
| Empty / whitespace-only line | `split` ⇒ 0 spans ⇒ line gets **0 segments**; no LLM candidate; not an error. |
| All-dialogue chapter | Every line a quote span; all sent as candidates; narrator only if narration present. |
| Unknown / ambiguous speaker | Model returns low confidence (or the stage defaults it) ⇒ `NEEDS_REVIEW`. |
| Model returns a `segment_id` not requested | Ignored (defensive map over requested ids only). |
| Model omits a requested `segment_id` | Defaulted to low-confidence narrator ⇒ `NEEDS_REVIEW`. |
| Malformed JSON / provider error | Provider raises `LLMProviderError`; stage saves partial, returns FAILED (re-runnable); status unset. (Alternative softer policy: flag that batch NEEDS_REVIEW and continue — pick one; recommend **FAILED** so a broken provider is loud, matching parse/correct.) |
| Mismatched/unbalanced quotes in a line | Whole line ⇒ 1 narration span (§3b rule 4); no crash. |
| Very long chapter | Windowed batches (§11) bound tokens; per-chapter progress. |
| Book with many characters | Registry grows; `known_speakers` may get large — acceptable v1; (future: cap/summarize). |
| Re-run (idempotency) | Lines with segments skipped; no new segments, no extra LLM calls. |
| `ctx.llm is None` | FAILED, status unset, re-runnable. |
| All segments narration (no quotes anywhere) | Zero LLM calls; stage still COMPLETED. |

---

## 13. Test plan (for the tester — fully offline/deterministic, no real API)

Add `tests/attribution/` (segmenter + policy unit tests) and
`tests/pipeline/test_attribute_stage.py` (integration). Add an `attribute_ready_project` fixture to
`conftest.py`: a saved project with `stage_status[CORRECT]=COMPLETED` (and PARSE) whose lines carry
**known** dialogue (a narrator-only line, a `"Hello," said Alice.` line, a `"Hi," Bob replied.`
line, an empty line) so assertions are exact, with `project.speakers` seeded with just a narrator
(or empty, to test narrator auto-creation).

Extend `FakeLLMProvider` only if needed: it already records `attribute_calls` and returns
scripted candidates (first = low-confidence). Add an optional `script: dict[str, tuple[str|None,
float]]` keyed by segment id (or by quote text) so a test can force exact name/confidence per
segment, **and** assert call count for idempotency/batching. Never construct a real provider.

### 13a. Segmenter unit tests (`tests/attribution/test_segmenter.py`)
- No-quote line ⇒ 1 narration span == whole line.
- `The narrator spoke. "Hello there," said Alice.` ⇒ `[narration, quote, narration]` with exact
  texts and `kind`s; quote text includes its delimiters.
- Curly quotes `“Hi,” said Bob.` split the same as straight.
- Two quotes in one line ⇒ two quote spans + interleaved narration.
- Unbalanced quote (`"oops`) ⇒ 1 narration span (whole line), no crash.
- Apostrophe/contraction (`Alice's "Hi," she said.`) ⇒ `'` not treated as a delimiter; only the
  `"…"` is a quote span.
- Empty/whitespace line ⇒ `[]`.
- **Determinism:** same input twice ⇒ identical spans (pin the cache-key contract).

### 13b. Policy unit tests (`tests/attribution/test_policy.py`)
- `review_status_for(0.9, 0.75) == APPROVED`; `review_status_for(0.5, 0.75) == NEEDS_REVIEW`;
  boundary `0.75 ⇒ APPROVED`.
- `ensure_narrator` creates one when absent, reuses the existing narrator when present (no dup).
- `resolve_speaker`: `None ⇒ narrator`; existing name (case-insensitive) reuses id; new name
  creates a `CHARACTER` speaker appended to the project.

### 13c. Stage integration tests (`tests/pipeline/test_attribute_stage.py`)
Run against saved `attribute_ready_project` with `tmp_workspace`, `FakeLLMProvider`,
`RecordingProgressReporter`; assert against the **reloaded** project (`tmp_workspace.load()` proves
persistence):
- Returns `StageResult(stage=ATTRIBUTE, status=COMPLETED)`;
  `stage_status[str(StageName.ATTRIBUTE)] == COMPLETED`.
- A narrator-only line ⇒ one Segment, `role=NARRATOR`, `review_status=APPROVED`, points at the
  narrator Speaker; **no** LLM candidate generated for it.
- A `"Hello," said Alice.` line ⇒ a narration segment (APPROVED narrator) + a quote segment whose
  `speaker_id` resolves to a Speaker named "Alice" with `role=CHARACTER`; confidence + status per
  the fake's scripted value (high ⇒ APPROVED; the fake's low-confidence first candidate ⇒
  `NEEDS_REVIEW` — assert the flag).
- **Speaker registry:** "Alice"/"Bob" created in `project.speakers` with `role=CHARACTER`,
  `voice_clip_id=None`; said twice ⇒ one Speaker (no dup).
- **Low confidence ⇒ NEEDS_REVIEW** explicitly (use the fake's low-confidence entry).
- **Idempotent re-run:** run twice; assert (a) segment count per line is unchanged (no dups),
  (b) `FakeLLMProvider.attribute_calls` length is the **same** after the 2nd run (no extra calls),
  (c) `Line.segments` ids stable.
- **Stop/resume:** `RecordingProgressReporter(stop after 1 chapter)` ⇒ STOPPED, partial persisted
  (first chapter has segments, later chapters don't), status not COMPLETED; resume ⇒ COMPLETED, all
  lines attributed, no double-attribution.
- **`ctx.llm is None` ⇒ FAILED**, `stage_status[ATTRIBUTE]` unset, re-runnable.
- **Provider unavailable** (`FakeLLMProvider(available=False)`) ⇒ FAILED.
- **Malformed/raising provider:** a fake whose `attribute_speakers` raises ⇒ FAILED, partial saved,
  status unset; OR (if softer policy chosen) those segments `NEEDS_REVIEW` and COMPLETED — test
  whichever §12 policy is confirmed.
- **Missing/extra returned ids:** a fake that omits an id ⇒ that segment defaults to NEEDS_REVIEW
  narrator; a fake that returns an unrequested id ⇒ ignored, no crash.
- **Empty line ⇒ 0 segments**; all-narration chapter ⇒ 0 LLM calls but COMPLETED.
- **`is_complete`/`next_stage`:** True after run; in `Pipeline([Parse, Correct, Attribute])`
  `next_stage` advances to `review`; re-run does not re-attribute.
- **Batching:** a chapter > `MAX_LINES_PER_BATCH` ⇒ assert `attribute_calls` count == number of
  windows.
- **Offline:** `StageContext` uses `FakeLLMProvider` only; no network; `tts` None. Tooling
  (`ruff`/`black`/`mypy src`/`pytest`) green; `tests/test_lazy_imports.py` extended so importing the
  attribution/stage modules pulls in **no** `anthropic`/`openai`.

### 13d. Claude provider unit test (only if §7b "implement Claude now" chosen)
`tests/providers/test_claude_attribution.py`: monkeypatch the lazily-imported `anthropic` client
with a fake returning canned JSON; assert `attribute_speakers` parses it into the right
`AttributionCandidate`s, maps `null`/`"narrator"` ⇒ narrator, enforces one-per-requested-id, and
raises `LLMProviderError` on non-JSON. **No real network.**

---

## 14. Ordered task list (coder)

Dependency order: 0 (decisions) → 1 → 2 → 3 → 4 (stage uses 1-3) → 5 (provider, optional-now) →
6/7 (tests/tooling).

0. **Get user sign-off** on §3b (segmentation approach + quote conventions), §6 (threshold 0.75 +
   COMPLETED), §11 (batch granularity 60), §5c (defer aliases), §7b (implement Claude now vs fake).
1. **Segmenter** — `attribution/segmenter.py`: `SegmentSpan`, `Segmenter` Protocol, `QuoteSegmenter`
   (§3b). Pure/offline/deterministic. Unit-test first.
2. **Policy** — `attribution/policy.py`: `ATTRIBUTION_CONFIDENCE_THRESHOLD`, `review_status_for`,
   `ensure_narrator`, `resolve_speaker` (§5/§6).
3. **Orchestration** — `attribution/attribute.py`: `build_context`, `attribute_chapter` (§10);
   `attribution/__init__.py` re-exports + `default_segmenter()`.
4. **SegmentAttributeStage** — fill `is_complete` + `run` (§10): None-guard `ctx.llm`, per-chapter
   loop with `should_stop` checkpoint + per-chapter save, skip attributed lines (§9), FAILED paths.
   `__init__(segmenter=None, threshold=...)`.
5. **Claude provider** (if chosen, §7b) — implement `attribute_speakers` + `is_available` in
   `providers/llm/claude.py` (lazy `anthropic`, strict-JSON prompt, parse → `AttributionCandidate`,
   `LLMProviderError`). Consult the `claude-api` skill. Add `LLMProviderError` to `errors.py`.
   (Optionally a `build_available_llm(config)` primary→backup helper in `providers/__init__.py`.)
6. **Fixtures + tests** — `attribute_ready_project` in `conftest.py`; extend `FakeLLMProvider`
   (scriptable per-segment); `tests/attribution/test_*.py`, `tests/pipeline/test_attribute_stage.py`,
   and (if §7b) `tests/providers/test_claude_attribution.py` (§13).
7. **Tooling** — `ruff check`, `black --check`, `mypy src`, `pytest` green; extend
   `tests/test_lazy_imports.py` so the new modules stay SDK-free at import.

---

## 15. Risks & open questions

- **Per-line segmentation can't see cross-line quotes** (multi-paragraph dialogue, §3b.5) — real
  limitation; deferred. Biggest accuracy gap; flag to user.
- **Em-dash / single-quote dialogue** (§3b.6/.7) unhandled in v1 — breaks for European/British
  typography. Confirm the target books' convention before locking the segmenter.
- **Weak local model accuracy** (LM Studio) — the conservative threshold + NEEDS_REVIEW backstop is
  the mitigation; the review UI (later stage) must be strong. Not this stage's concern but flag.
- **Alias/character-merge** (§5c) deferred — a book will spawn duplicate Speakers ("Darcy",
  "Mr. Darcy"); acceptable for v1, resolved later by user-driven merge.
- **FAILED-vs-soft-flag on malformed LLM output** (§12) — pick one; recommend FAILED (loud).
- **Scope creep guard:** do NOT build the review UI, voice-clip assignment, alias merging,
  cross-line quote tracking, or any audio/cache work here. This stage stops at: `Line.segments`
  populated + attributed, `project.speakers` grown, `stage_status[ATTRIBUTE]=COMPLETED`.

---

## 16. Verification strategy (what the tester must prove)

1. Deterministic segmentation: quotes ⇒ narration/quote spans, identical across runs (cache-key
   stability); no-quote ⇒ single narration; empty ⇒ zero segments.
2. Attribution mapping: each quote candidate ⇒ a Segment with the right `speaker_id`/`role`/
   `confidence`; narration ⇒ narrator APPROVED, never sent to the LLM.
3. Threshold policy: `>=0.75 ⇒ APPROVED`, `<0.75 ⇒ NEEDS_REVIEW`; defaults/missing ⇒ NEEDS_REVIEW.
4. Speaker registry: new names create CHARACTER speakers (voice_clip_id None); repeats reuse; a
   reserved narrator exists.
5. Idempotent re-run: no duplicate segments AND no extra LLM calls (assert `attribute_calls`).
6. Stop/resume: STOPPED persists partial, status not COMPLETED; resume completes, no double-attr.
7. Robustness: missing/extra/malformed LLM output handled per the confirmed policy; `ctx.llm None`
   ⇒ FAILED, status unset, re-runnable.
8. Persistence proven by reload; `stage_status[ATTRIBUTE]=COMPLETED`; `Line.text`/`suggestions`
   untouched; no schema bump.
9. Fully offline — `FakeLLMProvider` only, no network; new modules import no SDK.
10. `ruff` / `black` / `mypy src` / `pytest` green.

---

## Handoff

**Coder, once decisions land, build Task 1 first:** the deterministic `QuoteSegmenter` in
`src/casttrophizer/attribution/segmenter.py` (§3b) — it is the seam every other task depends on and
the piece that fixes the cache-key-stable segment boundaries. Unit-test it (§13a) before wiring the
policy, orchestration, or stage. The LLM is only ever reached via `ctx.llm.attribute_speakers` with
the **existing** signature (no provider-interface change); the stage is testable end-to-end against
`FakeLLMProvider`.

---

## DECISIONS FOR USER — CONFIRMED (build to these)

- **Segmentation approach & quote conventions (§3): CONFIRMED — deterministic offline
  `QuoteSegmenter`, straight + curly DOUBLE quotes only.** British single-quote, em-dash dialogue,
  and multi-paragraph (cross-line) quotes are **deferred**; unbalanced/unsupported lines fall back to
  a single narration span. (User confirmed their books use double-quote dialogue.)
- **Confidence threshold (§6): CONFIRMED — `ATTRIBUTION_CONFIDENCE_THRESHOLD = 0.75`.** Quote
  attributions `>= 0.75` start `APPROVED`, below start `NEEDS_REVIEW`; narration always APPROVED.
  Stage returns **COMPLETED** (not NEEDS_REVIEW). Make the threshold a module/config constant so it
  can be tuned later without code surgery.
- **Batch granularity (§11): CONFIRMED — per chapter, `MAX_LINES_PER_BATCH = 60`** (one LLM call per
  window).
- **Alias / character-name merging (§5c): CONFIRMED — defer.** Each distinct returned name is a
  distinct Speaker for v1; merging is a later user-driven action.
- **Real Claude provider (§7b): CONFIRMED — implement `ClaudeProvider.attribute_speakers` +
  `is_available` now.** `LMStudioProvider` stays a documented TODO. The stage + all tests run fully
  against `FakeLLMProvider` (no API in CI). The coder MUST take the Claude model id from config
  (`AppConfig`/`claude-opus-4-8`, env-overridable) and consult the `claude-api` skill for the correct
  Anthropic SDK usage — do not hardcode model ids or message shapes from memory.
- **(Minor) Malformed-LLM-output policy (§12): DECIDED (main agent) — retry once, then soft-flag.**
  On malformed/non-JSON or a missing/extra `segment_id`, retry the batch ONCE; if it still fails,
  do NOT fail the whole stage — mark that batch's quote segments `NEEDS_REVIEW` (confidence 0.0,
  narrator/`speaker_id=None` as a safe default) and continue, and surface it so the user reviews it.
  A genuinely unreachable provider (network/credentials, `is_available()` false mid-run) still
  returns **FAILED** (partial saved, re-runnable). Rationale: one bad batch must not kill a 40-chapter
  run, and nothing is silently committed (flagged for review).
