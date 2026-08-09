# Plan: Text Correction Stage (`CorrectTextStage`)

**Goal:** After parse, run a fully offline, deterministic correction pass over every
`Line.text` (OCR-artifact heuristics + spellcheck) that **auto-applies only high-confidence
fixes** and surfaces everything else as a `PENDING` `TextSuggestion` for later review — with
the original text always recoverable, and the pass safe to re-run. **No LLM is used here.**

Pipeline stage: **`correct`** (second stage; runs after `parse`, before `attribute`). It reads
parsed `Line`s, writes `Line.suggestions` and possibly mutates `Line.text`, persists via the
store, and sets `stage_status[CORRECT] = COMPLETED`. It does **not** touch `Line.segments`
(still empty — segmentation is the attribute stage's job).

---

## 1. Decisions locked by the scaffold (do not relitigate)

These are already fixed by existing code; honor them, don't redesign.

- `TextSuggestion(id, original, suggested, reason, confidence, status)` already exists in
  `domain/models.py` and is serialized in `domain/serialization.py`. `reason` is a free string
  (the docstring suggests `"ocr-artifact" | "spellcheck"`).
- `ReviewStatus` already has `AUTO_APPLIED`, `PENDING`, `APPROVED`, `REJECTED` — the exact set
  a suggestion cycles through. Use these; do not invent new statuses.
- `Line.suggestions: list[TextSuggestion]` defaults to `[]`. Suggestions attach to the **whole
  line**, not to a segment (segments don't exist yet at this stage).
- Stage contract (`pipeline/stage.py`): `run` must poll `ctx.progress.should_stop()` at safe
  checkpoints, return `STOPPED` with partial state persisted, be **idempotent/resumable**, and
  never import Qt. `is_complete` is status-driven off `stage_status`.
- `stage_status` keys are `str(StageName.X)` (see `parse.py` and `conftest.py`).
- The runner halts on `STOPPED / NEEDS_REVIEW / FAILED`. `COMPLETED` lets it proceed to
  `attribute`. **Correction does not block on user review** — it completes, and the user reviews
  the surfaced `PENDING` suggestions later (the dedicated `review` stage / UI). So
  `CorrectTextStage` returns `COMPLETED`, not `NEEDS_REVIEW`, even when it leaves pending
  suggestions. (Confirm this in §Open-questions — it's a product call.)
- The stage is offline: `StageContext.llm`/`tts` stay `None`. Call shape mirrors parse:
  `StageContext(store=..., progress=...)`.

---

## 2. Open questions — FLAG FOR USER before coding

The starred ones need a product/ownership decision before the coder starts.

1. **★★ Spellcheck library (DEPENDENCY DECISION — REQUIRED).** Spellcheck needs a bundled
   English dictionary and must run fully offline/deterministically in CI. Options compared below;
   I recommend **`pyspellchecker`** for v1. See §4 for the full comparison and rationale. **This
   adds a runtime dependency to `pyproject.toml` — the user must approve the specific library.**
   A clean fallback if the user wants **zero new deps for v1**: ship the OCR-heuristic pass only
   and defer dictionary spellcheck (the architecture in §6 keeps spellcheck a pluggable
   `Corrector`, so it can land later without reworking the stage). Confirm: *which library, or
   heuristics-only-for-now?*

2. **★★ Proper-noun / do-not-correct protection (REQUIRED).** A dictionary will flag character
   names ("Aelin", "Daenerys"), invented words, and dialect as misspellings. Auto-"correcting"
   them is exactly the irreversible-corruption failure the project forbids. Recommended layered
   defense (§7): (a) **never auto-apply** a pure-spellcheck change — spellcheck only ever
   produces `PENDING` suggestions; (b) maintain a per-project **allowlist** of protected tokens
   that suppresses suggestions entirely; (c) seed that allowlist automatically from
   capitalized-token frequency in the book (a token Capitalized & appearing ≥ N times is treated
   as a likely proper noun and skipped). Confirm: *allowlist + capitalization heuristic, and is
   the allowlist auto-seeded, user-edited, or both?* (Auto-seed for v1, user-editable later.)

3. **★ Does `correct` return `COMPLETED` or `NEEDS_REVIEW`?** Recommended **`COMPLETED`** so the
   pipeline flows to `attribute`; pending text suggestions are surfaced in the `review` stage/UI,
   not as a pipeline halt. (If correction should *force* a text-review gate before attribution,
   it would return `NEEDS_REVIEW` — but that conflates two concerns and blocks the cheap stages
   on a human. Recommend COMPLETED.) Confirm.

4. **★ Schema bump for reversibility metadata?** `TextSuggestion` as-is stores `original` and
   `suggested` strings but **not** a flag distinguishing "this fix mutated `Line.text`" from "this
   is a not-yet-applied suggestion", nor a stable signal for idempotency. `status` mostly covers
   this (`AUTO_APPLIED` ⇒ text was mutated; `PENDING` ⇒ not), so **v1 needs no schema change**
   (§5 shows how `status` alone suffices). One *optional* field — a per-line `corrected: bool`
   marker or storing the pre-correction text — would make idempotency bulletproof but costs a
   `schema_version` bump + migration. Recommend **no schema change for v1**; rely on the
   suggestion list as the source of truth (§5/§8). Flag if the user wants the stronger guarantee.

If the user picks "heuristics-only" on (1), the coder drops the spellcheck `Corrector` and its
test cases; everything else stands.

---

## 3. Where this sits in the pipeline & what it reads/writes

| | |
|---|---|
| **Reads** | `project.book.chapters[*].lines[*].text` (populated by parse). |
| **Writes** | `Line.suggestions` (appends `AUTO_APPLIED` + `PENDING` entries); `Line.text` (mutated only for high-confidence/auto-applied fixes). `Line.segments` untouched (stays `[]`). |
| **Persists** | whole `Project` via `ctx.store.save` (atomic). |
| **Records** | `stage_status[str(StageName.CORRECT)] = COMPLETED`. |
| **Progress** | per **chapter** (`set_total(n_chapters)`, one `advance` each) — matches parse and keeps the UI smooth on large books without per-line chatter. |

---

## 4. Correction sources / heuristics (the actual checks)

Two independent **correctors** behind one interface (§6). Each corrector inspects a line and
yields zero or more *candidate fixes*, each with a `reason` and a `confidence`. The stage policy
(§5) decides auto-apply vs. surface.

### 4a. OCR-artifact heuristics (`reason="ocr-artifact"`) — deterministic, no dictionary needed

These are pure string transforms. The ones that change *characters within a word* are only
proposed when the **result is a real dictionary word and the input is not** (so they need the
dictionary too — see note); the pure-typography ones need no dictionary.

| Heuristic | Example | Default disposition |
|---|---|---|
| **Doubled/である repeated-space collapse** | `"the  cat"` → `"the cat"` | **AUTO** (typographically safe, reversible-in-meaning). |
| **Smart-quote / dash normalization** *(only if enabled — see note)* | `"“hi”"` → `'"hi"'`, `"—"`/`"--"` → em-dash policy | **Surface** by default; see note. |
| **De-hyphenation across line break** | `"care-\nfully"` / `"care- fully"` → `"carefully"` *iff* `"carefully"` is a dictionary word and `"care"`+`"fully"` aren't a legit open compound | **Surface** (PENDING) — joining is meaning-bearing and easy to get wrong. |
| **Stray mid-word punctuation** | `"ca,t"`, `"wor.d"` → `"cat"`, `"word"` *iff* the stripped form is a dictionary word | **Surface**. |
| **`rn`↔`m`, `l`↔`1`, `0`↔`O`, `cl`↔`d` confusion** | `"modem"`→`"modern"`? `"l"`→`"1"`? | **Surface only**, and **only** when exactly one substitution turns a non-word into a dictionary word; never auto-apply (too ambiguous — `"modem"` is itself a word). |

> **Important:** the character-confusion and stray-punctuation heuristics are gated on "result is
> a dictionary word", so they depend on the spellcheck dictionary. If the user chooses
> heuristics-only (no dict), these specific heuristics degrade to **disabled** (can't verify the
> result is a word) and only the dictionary-free ones (space collapse, optionally
> de-hyphenation by a small built-in word list, quote/dash normalization) remain. Document this.

> **Smart-quote/dash normalization note:** Chatterbox TTS reads text, not glyphs; curly quotes
> and em dashes are *fine to speak* and carry no error. Normalizing them is cosmetic and risks
> churn. **Recommend: do NOT normalize quotes/dashes by default** (leave them; the parser already
> preserves Unicode per `make_epub_unicode`). Offer it only as an opt-in heuristic. This avoids
> generating thousands of no-value suggestions on a clean ebook. (Flag as a minor default.)

### 4b. Spellcheck (`reason="spellcheck"`) — needs a bundled dictionary

For each whitespace/punctuation-delimited token not protected (§7), if it is unknown to the
dictionary, propose the dictionary's top correction as a **PENDING** suggestion (never auto).
Tokenization, casing, and protection rules in §7.

### 4c. Spellcheck library comparison (★ user decision)

| Library | Offline / bundled dict | Pure-Python | Dep weight | Accuracy / speed | Notes |
|---|---|---|---|---|---|
| **`pyspellchecker`** *(recommended)* | Yes — ships a bundled freq dictionary, fully offline | Yes (no C build) | Light (one pure-Python wheel + a gzipped dict) | Levenshtein-based; fine for typo/OCR fixes; adequate speed for batch use with caching | Simple `WordFrequency` API; easy to mock; lets us add book-specific words to the known set (helps §7 proper-noun protection) |
| `symspellpy` | Needs a dictionary file shipped/downloaded; offline once present | Yes | Light–medium | **Much faster** (SymSpell), good for huge books | Slightly more setup (load a dictionary distance file); also pure-python |
| system `hunspell` (`cyhunspell`/`pyhunspell`) | Best accuracy (real morphology), but needs system `.dic`/`.aff` and a **C build** | No (C extension) | Heavy / platform-fragile on Windows | Best quality | Conflicts with "light dev/CI install" and the Windows target; avoid for v1 |

**Recommendation:** `pyspellchecker` for v1 — pure-Python, bundled dictionary, no build step,
trivially mockable, and its API lets us inject book-specific known words for proper-noun
protection. `symspellpy` is the upgrade path if spellcheck throughput becomes a bottleneck on
very large books. **`hunspell` is rejected** (C build, Windows-fragile, violates the light-install
convention). **The user must approve adding `pyspellchecker>=0.8` (or chosen alt) to
`pyproject.toml` `dependencies`.**

> Keep the dictionary load **lazy** (inside the corrector, behind the `Corrector` interface) and
> behind the offline-import test (`tests/test_lazy_imports.py` style) so importing the stage
> module stays cheap and Qt-free.

---

## 5. Auto-apply vs. surface policy (the "high-confidence" bar)

Per "nothing irreversible is automatic", the bar to **mutate `Line.text`** is deliberately high.

**AUTO_APPLY (mutate `Line.text`, record `TextSuggestion(status=AUTO_APPLIED)`) only when ALL hold:**
- The fix is **typographically meaning-preserving**, not lexical guessing. The only auto-apply
  class in v1 is **whitespace normalization** (collapse runs of spaces/tabs, trim, collapse
  space-before-punctuation like `"word ."` → `"word."`).
- The transform is **unconditionally reversible** from `original` (we store the pre-fix text).
- It does **not** depend on a dictionary lookup being "probably right".

**SURFACE as `PENDING` (do NOT mutate `Line.text`) for everything else**, specifically:
- **All spellcheck corrections** (a dictionary's "top pick" is a guess; names/dialect/jargon).
- **All character-confusion / stray-punctuation / de-hyphenation OCR fixes** (ambiguous; a
  plausible real word on each side).
- Quote/dash normalization if that heuristic is enabled.

Examples on each side of the line:

| Input line | Disposition | Resulting `Line.text` |
|---|---|---|
| `"He  said , hello"` (double space, space-before-comma) | **AUTO** | `"He said, hello"` (+ AUTO_APPLIED suggestion) |
| `"The narrarator spoke."` (`narrarator`) | **SURFACE** spellcheck → `narrator` | unchanged; PENDING suggestion |
| `"care-\nfully chosen"` | **SURFACE** de-hyphenation → `carefully` | unchanged; PENDING suggestion |
| `"modem art"` (OCR `rn`→`m`?) | **SURFACE** low-confidence → `modern`? | unchanged; PENDING (and only if `modem`/`modern` ambiguity rules fire) |
| `"Aelin drew her blade."` (`Aelin` unknown) | **PROTECTED** (proper noun, §7) | unchanged; **no** suggestion |

Confidence values are advisory metadata for the review UI, not the gate. Suggested conventions:
whitespace auto-fixes `confidence ≈ 0.99`; spellcheck single-edit `≈ 0.6–0.8`; multi-edit or
OCR-confusion `≈ 0.3–0.5`. The **status** (AUTO_APPLIED vs PENDING), not the float, determines
whether text was mutated.

---

## 6. Data model, reversibility & the corrector interface

### 6a. Reversibility — how a fix is recorded

- **AUTO_APPLIED fix:** mutate `Line.text` to the corrected string **and** append
  `TextSuggestion(original=<pre-fix line text or token>, suggested=<post-fix>, reason=...,
  confidence=..., status=AUTO_APPLIED)`. `original` is the recovery key: the review UI can undo
  by restoring `original`. **Decision:** for whole-line typographic fixes, store the **whole
  line's pre-fix and post-fix text** in `original`/`suggested` (one suggestion per applied
  line-level transform), so undo is a clean line-text swap with no offset bookkeeping.
- **PENDING suggestion:** append `TextSuggestion(original=<token or line>, suggested=<proposal>,
  reason, confidence, status=PENDING)` and **leave `Line.text` unchanged**. Accept/reject is the
  review stage's job (it will set `APPROVED`/`REJECTED` and, on approve, apply `suggested`).

This uses the existing five `TextSuggestion` fields only. **No new fields required for v1.**

> **Granularity decision (token vs. line `original`):** spellcheck/OCR token suggestions store
> the **token** in `original`/`suggested` (e.g. `original="narrarator"`, `suggested="narrator"`)
> so the review UI can apply a targeted token replacement; auto-applied **line-level** typographic
> fixes store the **whole line**. The `reason` string disambiguates which kind it is. If the user
> later wants character offsets for precise in-place token replacement, that's an additive field
> + schema bump — defer (§2.4).

### 6b. Corrector interface (keeps checks pluggable & unit-testable)

New module `src/casttrophizer/text/` (mirrors how `ebook/` isolates its concern):

```python
# src/casttrophizer/text/base.py
@dataclass(frozen=True)
class FixCandidate:
    original: str        # the token or line text being changed
    suggested: str       # the proposed replacement
    reason: str          # "ocr-artifact" | "spellcheck"
    confidence: float
    auto: bool           # True => eligible for auto-apply (whitespace only, in v1)

class Corrector(Protocol):
    def line_fixes(self, text: str, *, protected: frozenset[str]) -> list[FixCandidate]:
        """Return candidate fixes for one line's text. Pure, deterministic, offline."""
        ...
```

Concrete correctors:
- `src/casttrophizer/text/ocr.py` → `OcrHeuristicCorrector` (§4a; whitespace fixes carry
  `auto=True`, the rest `auto=False`). Dictionary-gated heuristics take an injected
  `is_word: Callable[[str], bool]` so they're testable without the real dictionary.
- `src/casttrophizer/text/spelling.py` → `SpellcheckCorrector` (§4b; always `auto=False`).
  Wraps `pyspellchecker` behind the interface, lazy import, accepts an injected known-words set
  (for §7 protection) and is mockable (tests inject a fake corrector, never load the real dict).

A small assembler `apply_fixes(line, candidates, protected) -> None` mutates the line:
auto candidates are applied to `Line.text` (in a defined order, left-to-right, idempotently) and
recorded `AUTO_APPLIED`; non-auto become `PENDING` suggestions. This keeps `CorrectTextStage.run`
thin and the policy in one tested place.

### 6c. No domain/serialization changes

`domain/models.py` and `domain/serialization.py` are **sufficient as-is**. No `schema_version`
bump. (Confirmed against `_suggestion_to_dict`/`_suggestion_from_dict` — they already round-trip
all five fields and `status` via `ReviewStatus`.)

---

## 7. Idempotency, re-run & proper-noun protection (the critical correctness section)

### 7a. Idempotency / re-run

`run` must be safe to call twice, and safe to call again after the user edits text or
accepts/rejects suggestions. Rules:

1. **Process a line only if it has no prior correction record for the current pass.** Determine
   "already corrected" by inspecting `Line.suggestions`: a line that already carries any
   suggestion with `reason in {"ocr-artifact","spellcheck"}` (any status) has been processed —
   **skip it** on re-run. This is the idempotency key and needs no new field.
2. **Never re-apply an auto fix.** Because auto fixes are recorded as `AUTO_APPLIED` suggestions,
   rule 1 prevents a second pass from re-collapsing/re-mutating. Even if it didn't, whitespace
   collapse is idempotent by construction (collapsing already-single spaces is a no-op).
3. **Never resurrect a rejected suggestion.** If the user `REJECTED` a suggestion, rule 1 still
   skips the line, so it won't be re-proposed. (If the user *edited* the text and wants a fresh
   pass, that's an explicit "re-run correction on this line" action in the UI which clears the
   line's prior suggestions first — out of scope for this stage, but the skip rule makes the
   default safe.)
4. **Resume after STOP:** lines already processed carry suggestions and are skipped; the pass
   continues from the first unprocessed line. So a `STOPPED → resume` does **not** double-apply.

> This "skip lines that already have correction suggestions" rule is the whole idempotency story
> and is why no schema change is needed. The tester must pin it (run twice, assert suggestion
> count and `Line.text` are stable).

### 7b. Proper-noun / do-not-correct protection (★ user decision, §2.2)

Layered, before any spellcheck suggestion is emitted:

1. **Spellcheck never auto-applies** (policy §5) — worst case a name becomes a *surfaced* PENDING
   suggestion the user rejects, never a silent mutation.
2. **Protected token set** passed into every corrector as `protected: frozenset[str]`
   (case-folded). A token in `protected` yields no suggestion.
3. **Auto-seed `protected` per book**, computed once at the start of `run` from the parsed text:
   - Tokens that are **Capitalized mid-sentence** or appear capitalized **≥ N times** (default
     `N=2`) and are **not** sentence-initial-only → treated as proper nouns.
   - Optionally union the project's speaker names (`project.speakers[*].name`) — character names
     are exactly what we must not "correct". (Cheap, high-value; recommend including.)
   - All-caps tokens, tokens with digits, and tokens shorter than 3 chars → skipped from
     spellcheck (acronyms/IDs/initials).
4. **Dictionary augmentation:** feed the auto-seeded set into the spellchecker's known-words
   (`pyspellchecker.word_frequency.load_words(...)`) so they're not even flagged.

Edge cases this also covers: dialect/intentional misspellings recur and get capitalized/frequent
→ likely protected or at least only surfaced; dictionary-lacking words → surfaced, never
auto-applied; hyphenated compounds → tokenized as a unit first, only split-checked by the
de-hyphenation heuristic.

---

## 8. Stage wiring (`src/casttrophizer/pipeline/stages/correct.py`)

### 8a. `is_complete`

```python
def is_complete(self, project: Project) -> bool:
    return project.stage_status.get(str(StageName.CORRECT)) == ReviewStatus.COMPLETED
```

Purely status-driven, exactly like `ParseStage` (don't also require suggestions to exist — a
perfectly clean book legitimately produces zero suggestions).

### 8b. `run` algorithm

```python
def run(self, project: Project, ctx: StageContext) -> StageResult:
    ctx.progress.message("Correcting text")
    try:
        correctors = self._correctors or default_correctors()   # injectable for tests
        protected = build_protected_set(project)                # §7b, computed once
    except Exception as exc:                                     # dict load / corrector init
        return StageResult(self.name, ReviewStatus.FAILED, str(exc))

    chapters = project.book.chapters
    ctx.progress.set_total(len(chapters))

    for chapter in chapters:
        if ctx.progress.should_stop():
            ctx.store.save(project)                              # partial state persisted
            return StageResult(self.name, ReviewStatus.STOPPED, "stopped during correct")

        for line in chapter.lines:
            if _already_corrected(line):                         # §7a rule 1 — idempotent skip
                continue
            candidates = []
            for corrector in correctors:
                candidates += corrector.line_fixes(line.text, protected=protected)
            apply_fixes(line, candidates)                        # §6b: mutate text + record
        ctx.progress.advance(1, message=chapter.title)

    project.stage_status[str(StageName.CORRECT)] = ReviewStatus.COMPLETED
    ctx.store.save(project)
    return StageResult(self.name, ReviewStatus.COMPLETED, "text corrected")
```

Conventions matched to `parse.py`:
- `__init__(self, correctors: list[Corrector] | None = None)` for test injection (mirrors
  `ParseStage(parser=...)`).
- Stop check **per chapter** (the natural checkpoint); partial state saved; status left
  not-COMPLETED so resume continues. Because already-processed lines are skipped (§7a), resume is
  cheap and correct even though `run` re-enters from the top.
- **FAILED path:** if the dictionary/library fails to load (e.g. user picked the lib but it's
  missing) or a corrector raises, return `FAILED` and **do not** set `stage_status` — so a fixed
  environment can re-run. Mirrors parse's `EbookParseError → FAILED`.
- **COMPLETED**, not NEEDS_REVIEW (§2.3), so the pipeline proceeds to attribute.
- Empty book / chapter with `lines == []` → zero work, still COMPLETED.
- Qt-free; `llm`/`tts` unused.

### 8c. Module / file layout

| Path | Purpose |
|---|---|
| `src/casttrophizer/text/__init__.py` | `default_correctors()` factory + re-exports; keeps imports lazy. |
| `src/casttrophizer/text/base.py` | `FixCandidate`, `Corrector` protocol, `apply_fixes`, `build_protected_set`. |
| `src/casttrophizer/text/ocr.py` | `OcrHeuristicCorrector` (§4a). |
| `src/casttrophizer/text/spelling.py` | `SpellcheckCorrector` wrapping the chosen lib (lazy import). |
| `src/casttrophizer/pipeline/stages/correct.py` | fill in `CorrectTextStage.run`/`is_complete` (§8a/§8b). |

---

## 9. Edge cases (stage must handle; tester must cover)

| Case | Required behavior |
|---|---|
| Proper nouns / character names | Protected (§7b): no suggestion, never auto-applied. |
| Dialect / intentional misspelling | Surfaced at most (PENDING), never auto; recurring+capitalized → protected. |
| Word the dictionary lacks (jargon/coined) | Surfaced PENDING, never auto-mutated. |
| All-caps token (`NASA`, `OK`) | Skipped from spellcheck. |
| Numbers / alphanumerics (`Room101`, `3rd`) | Skipped from spellcheck. |
| Hyphenated compound (`well-known`) | Tokenized as a unit; only the de-hyphenation heuristic may propose joining (PENDING). |
| Empty / heading / whitespace-only line | No-op; produces no suggestions; not skipped-as-error. |
| Line that's pure punctuation/quotes | No spellcheck tokens; whitespace fix may still apply. |
| Already-corrected line (re-run) | Skipped via §7a rule 1; counts/text stable. |
| Very large book (thousands of lines) | Lazy-load dict **once**; build `protected` once; cache word lookups; per-chapter progress only. Recommend `symspellpy` as the upgrade if throughput bites — `pyspellchecker` is acceptable for v1 with caching. |
| Unicode (smart quotes, accents) | Preserved; quote/dash normalization off by default (§4a note) so no churn. |
| Dictionary/library missing or fails | `run` → FAILED, `stage_status` unset, re-runnable. |

---

## 10. Test plan (for the tester — all offline, deterministic, no real models/network)

Add `tests/text/` (unit tests for correctors) and `tests/pipeline/test_correct_stage.py`
(integration). Reuse `tmp_workspace`, `RecordingProgressReporter`, `StopAfterNProgress`. Add a
`correct_ready_project` fixture in `conftest.py`: a saved project with `stage_status[PARSE]=
COMPLETED` and a handful of `Line`s carrying **planted** errors (double spaces, a misspelling, a
protected name, a clean line) so assertions are exact. **Never load the real dictionary in
tests** — inject a `FakeSpellchecker`/`FakeCorrector` (a tiny known-words set + a fixed
correction map), exactly as parse tests inject a `CountingParser`.

### 10a. Corrector unit tests (`tests/text/test_ocr.py`, `tests/text/test_spelling.py`)
- Whitespace fix produces an `auto=True` `FixCandidate`; `"a  b"` → `"a b"`; idempotent on
  `"a b"` (no candidate).
- Space-before-punctuation collapse: `"word ."` → `"word."`, `auto=True`.
- OCR confusion heuristic: with an injected `is_word`, `"modem"`→`"modern"` only surfaces when the
  rules fire, always `auto=False`; never proposed when result isn't a word.
- De-hyphenation: `"care-fully"` → `"carefully"` only if `is_word("carefully")`; `auto=False`.
- Spellcheck: unknown token → one `auto=False` candidate with the fake's correction; known token
  → none; **protected** token → none; all-caps/number/short token → none.

### 10b. `apply_fixes` / policy tests (`tests/text/test_apply.py`)
- An `auto=True` candidate **mutates `Line.text`** and records `AUTO_APPLIED` with recoverable
  `original`.
- An `auto=False` candidate **leaves `Line.text` unchanged** and records `PENDING`.
- Original text is recoverable from the `AUTO_APPLIED` suggestion's `original`.

### 10c. `CorrectTextStage` integration (`tests/pipeline/test_correct_stage.py`)
Run against the saved `correct_ready_project` with `tmp_workspace`; assert against the **reloaded**
project (`tmp_workspace.load()` — proves persistence):
- Returns `StageResult(stage=CORRECT, status=COMPLETED)`; `stage_status[str(StageName.CORRECT)]
  == COMPLETED`.
- A planted double-space line is **auto-fixed** in reloaded `Line.text`, with an `AUTO_APPLIED`
  suggestion whose `original` recovers the text.
- A planted misspelling is **surfaced** as a `PENDING` suggestion and `Line.text` is **unchanged**.
- A planted protected name produces **no** suggestion and is unchanged.
- A clean line produces **no** suggestion.
- **`Line.segments` stays `[]`** on every line (assert explicitly — correct must not touch them).
- **Idempotent re-run:** run the stage twice; suggestion counts and every `Line.text` are
  identical after the second run (use a counting/fake corrector to assert lines aren't
  re-processed, or assert stable suggestion ids/counts).
- **Stop/resume:** `StopAfterNProgress(n=1)` → `STOPPED`, partial persisted, status not COMPLETED;
  resume → COMPLETED, all lines processed, no double-applied fixes.
- **is_complete / next_stage:** `is_complete` True after run; in a `Pipeline([ParseStage(),
  CorrectTextStage()])`, `next_stage` advances to attribute (or the next configured stage), and a
  re-run does not re-correct.
- **Read-only source:** source EPUB bytes/mtime unchanged; only `project.json` written.
- **Failure path:** inject a corrector whose init/`line_fixes` raises → `FAILED`,
  `stage_status[CORRECT]` unset, re-runnable.
- **Progress:** `set_total(n_chapters)` and one `advance` per chapter.
- **Offline:** `StageContext.llm`/`tts` are `None`; no network; tooling (`ruff`/`black`/`mypy
  src`/`pytest`) green.

---

## 11. Ordered task list (coder)

Dependency order: 0 (user decisions) → 1 → 2 → 3 → 4 (stage uses correctors) → 5/6 (tests/tooling).

0. **Get user sign-off** on §2.1 (spellcheck library / heuristics-only) and §2.2 (proper-noun
   protection). If a library is approved, add it to `pyproject.toml` `dependencies` and the
   `mypy` untyped-imports override list (like `ebooklib`/`bs4`).
1. **Corrector interface** — `src/casttrophizer/text/base.py`: `FixCandidate`, `Corrector`
   protocol, `apply_fixes`, `build_protected_set` (§6b, §7b). Pure/offline.
2. **OCR corrector** — `src/casttrophizer/text/ocr.py` (§4a). Whitespace fixes `auto=True`;
   dictionary-gated heuristics take an injected `is_word`. `auto=False` for everything non-trivial.
3. **Spellcheck corrector** — `src/casttrophizer/text/spelling.py` (§4b): wrap the chosen lib,
   lazy import, inject known/protected words, always `auto=False`. Skip if heuristics-only chosen.
4. **`default_correctors()`** — `src/casttrophizer/text/__init__.py`.
5. **CorrectTextStage** — fill `is_complete` + `run` in
   `src/casttrophizer/pipeline/stages/correct.py` (§8): per-chapter progress, `should_stop`
   checkpoint, idempotent skip (§7a), persist via `ctx.store.save`, set `stage_status[CORRECT]=
   COMPLETED`, FAILED on corrector/dict failure. `__init__(correctors=None)` for test injection.
6. **Fixtures + tests** — `correct_ready_project` in `conftest.py`; `FakeSpellchecker`/
   `FakeCorrector` in `tests/fakes/`; `tests/text/test_*.py` and
   `tests/pipeline/test_correct_stage.py` (§10).
7. **Tooling** — `ruff check`, `black --check`, `mypy src`, `pytest` green; extend
   `tests/test_lazy_imports.py` so importing the stage/text modules doesn't import the dict eagerly.

---

## 12. Risks & scope guard

- **Suggestion volume / noise.** A real ebook can produce thousands of spellcheck suggestions
  (every name, place, coined term). If the review UX drowns, correction is worse than useless.
  Mitigation: aggressive protection (§7b), no quote/dash normalization by default, never
  auto-apply lexical guesses. This is the biggest product risk — flag to the user.
- **Proper-noun false positives** are the dangerous failure (silent corruption of a character
  name). The "spellcheck never auto-applies" rule (§5) is the hard backstop; the allowlist is the
  softener. Tester must pin a protected-name case.
- **Dictionary determinism in CI.** Tests must mock the dictionary; a real `pyspellchecker` load
  is slow and version-sensitive. The injected-corrector pattern (mirroring `CountingParser`) keeps
  CI offline and fast.
- **Performance on large books** — `pyspellchecker` is O(edit-distance) per unknown token; cache
  lookups and build `protected` once. `symspellpy` is the escape hatch if needed (interface makes
  the swap local to `spelling.py`).
- **Scope creep guard:** do **NOT** implement segmentation, attribution, or the review *UI* here.
  This stage stops at: `Line.text` (whitespace-only auto-fixes) + `Line.suggestions` (PENDING for
  everything else) + `stage_status[CORRECT]=COMPLETED`. `Line.segments` stays `[]`.

---

## 13. Verification strategy (what the tester must prove)

1. High-confidence (whitespace) fixes **auto-apply** and mutate `Line.text`; original recoverable
   from the `AUTO_APPLIED` suggestion.
2. Ambiguous OCR fixes and **all spellcheck** fixes are surfaced as **PENDING**, `Line.text`
   unchanged.
3. Proper nouns / protected tokens get **no** suggestion and are unchanged.
4. **Idempotent re-run:** second run changes nothing (counts + text stable; lines not
   re-processed).
5. **Stop/resume:** STOPPED persists partial state, status not COMPLETED; resume completes with
   no double-apply.
6. Persistence proven by reload; `stage_status[CORRECT]=COMPLETED`; `Line.segments == []`.
7. Failure path → FAILED, status unset, re-runnable.
8. Fully offline — dictionary mocked, `llm`/`tts` `None`, no network.
9. `ruff` / `black` / `mypy src` / `pytest` green; stage/text imports stay lazy.

---

## Handoff

**Decisions — CONFIRMED by the user (build to these):**
1. **Spellcheck library** (§2.1 / §4c): **`pyspellchecker`** — add as a core `pyproject.toml`
   dependency (pure-Python, bundled dict, offline, mockable).
2. **Auto-apply bar** (§5): **whitespace/typography only.** ALL OCR character-confusion and ALL
   spellcheck corrections are surfaced as PENDING — nothing lexical is auto-applied. (De-hyphenation
   is surfaced, not auto-joined.)
3. **Proper-noun protection** (§2.2 / §7b): **layered** — spellcheck never auto-applies, PLUS a
   per-project allowlist auto-seeded from high-frequency capitalized tokens AND `project.speakers`
   names.
4. (Minor) `correct` returns **COMPLETED**, not NEEDS_REVIEW (§2.3); no schema bump (§2.4).

**Coder, once decisions land, build Task 1 first:** the corrector interface in
`src/casttrophizer/text/base.py` (`FixCandidate`, `Corrector`, `apply_fixes`,
`build_protected_set`) — it's the seam every other task depends on and is the piece that encodes
the auto-vs-surface policy and reversibility contract. Unit-test it with injected `is_word` before
wiring any real dictionary or the stage.
