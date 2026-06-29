# Plan: EPUB Parse Stage

**Goal:** Turn the source EPUB referenced by a project into a persisted `Book → Chapter → Line`
tree — `EpubParser.parse` reads the EPUB via `ebooklib`+`BeautifulSoup`, `ParseStage.run`
maps the result into `project.book`, persists it via the workspace store, and marks
`stage_status[PARSE] = COMPLETED` so the pipeline can resume.

Pipeline stage: **`parse`** (the first stage). Output feeds `correct` (text suggestions on
`Line.text`) and `attribute` (segmentation of each `Line` into `Segment`s). This plan stops at
populated `Line`s; **segmentation is deferred to the attribute stage** (decision §4).

---

## 1. Decisions locked by the scaffold (do not relitigate)

These are already fixed by existing code; the coder must honor them, not redesign them.

- `EbookParser.parse(path) -> ParsedBook` and the `ParsedBook` / `ParsedChapter` DTOs already
  exist in `src/casttrophizer/ebook/base.py`. Parse output is **plain-text lines** —
  `ParsedChapter.lines: list[str]`. No HTML survives the parser boundary.
- `ParseStage.run(project, ctx)` receives an **already-loaded `Project`** (the runner calls
  `ctx.store.load()`). The project's `book.source_ebook_path` is the input; `book.title`,
  `book.author`, and `book.chapters` are to be filled by this stage.
- Resume is driven by `project.stage_status[StageName.PARSE]`, persisted in `project.json`.
- The source EPUB is **read-only**; the parser must not write into the workspace.
- TTS render unit is the **Segment**; `audio_cache_key`/`audio_status` live on `Segment`, not
  `Line` (already reflected in `domain/models.py`).

---

## 2. Open questions — FLAG FOR USER before coding

These need a product decision; I recommend a default for each but the main agent should confirm
the starred ones with the user.

1. **★ Line-splitting granularity.** Recommended: **one `Line` per block-level element**
   (paragraph `<p>`, heading, list item, blockquote). A `Line` is *not* split into segments here.
   Rationale: a paragraph is the natural review unit and matches how `correct` surfaces text fixes;
   sub-paragraph segmentation (narration vs. inline quote) is the *attribute* stage's job.
   The alternative (sentence-level lines) bloats the line count, fragments quotes across lines, and
   pre-empts the segmenter. **Confirm: paragraph-level lines, not sentence-level.**

2. **★ Heading handling — DECIDED (user): narrate headings too.** The chapter title is derived
   from the heading/ToC (§3) AND the heading is **also emitted as a `Line`** (narrator role) so it is
   spoken in the audiobook. Lines are emitted in **document order**: since well-formed chapters open
   with their heading, that line is normally first and equals `Chapter.title`, but the parser does
   **not** force-reorder the heading ahead of any genuine pre-heading content (e.g. a part-opener
   epigraph) — doing so would corrupt reading order. The same heading text appears both as
   `Chapter.title`/M4B marker and as a spoken line. (Not dropped.)

3. **Footnotes / endnotes.** Recommended for v1: **strip footnote reference markers**
   (superscript anchors like `<a epub:type="noteref">`) from line text and **drop** the footnote
   body sections (`epub:type="footnote"/"endnote"` or `<aside>`). Surfacing footnotes as spoken
   content is out of scope for the first parse. Flag as a known limitation, not a blocker.

4. **Front/back matter.** Recommended: include **all linear spine documents** as chapters in
   reading order (title page, dedication, etc. become their own short "chapters"). Do **not** try to
   classify front matter in v1 — that is heuristic and error-prone. The user can ignore/merge later
   in review. Skip only non-linear spine items (`linear="no"`) and the nav document itself.

If the user wants different behavior on 1/2, that changes the line-emission rule the coder writes,
so confirm before the coder starts.

---

## 3. EpubParser implementation (`src/casttrophizer/ebook/epub.py`)

Fill in `EpubParser.parse`. Keep `ebooklib`/`bs4` imports **lazy inside `parse`** (the module
docstring already promises this). On any failure raise `EbookParseError` (already in `errors.py`).

### 3a. Reading order (spine)

```python
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

book = epub.read_epub(str(path))
```

- Iterate `book.spine` (list of `(idref, linear)` tuples). For each idref, resolve the item via
  `book.get_item_with_id(idref)`.
- **Skip** items that are not `ebooklib.ITEM_DOCUMENT`, the nav document
  (`item.get_name()` is the nav, or `properties` contains `"nav"`), and items with `linear == "no"`.
- This yields the ordered list of content documents = chapter candidates.

> Use the spine for *order* (it is the authoritative reading order). Use the ToC/nav only for
> *titles* (§3c). Do not enumerate `book.get_items_of_type(ITEM_DOCUMENT)` directly — that order is
> not guaranteed to be reading order.

### 3b. HTML → text per document

For each content document:
```python
soup = BeautifulSoup(item.get_content(), "html.parser")
body = soup.body or soup
```
- Remove non-spoken nodes: `script`, `style`, and footnote/endnote sections
  (`[epub\\:type=footnote]`, `[epub\\:type=endnote]`, `[role=doc-footnote]`, `[role=doc-endnote]`,
  and bare `<aside>` used for notes). Use `for n in soup.select(...): n.decompose()`.
- Remove footnote reference anchors (`a[epub\\:type=noteref]`, `a[role=doc-noteref]`) so the marker
  digit does not leak into line text.
- Define block elements to emit as lines: `p`, `h1`–`h6`, `li`, `blockquote`. Iterate them in
  document order (`body.find_all([...])`), and for each:
  - `text = element.get_text(separator=" ", strip=True)`
  - Collapse internal whitespace (`re.sub(r"\\s+", " ", text).strip()`).
  - **Skip empties** (e.g. spacer `<p></p>`, image-only `<p><img/></p>`).
- **Heading detection for the title:** the first `h1`–`h6` (or ToC title — §3c) becomes
  `ParsedChapter.title`; per decision §2.2 the heading is **also emitted as a `Line`** (narrator) in
  document order so it is spoken — normally the first line, but not force-reordered. The title-source
  heading is therefore kept, not excluded; emit blocks in document order and reuse the heading text
  as the title.
- Guard against nested blocks producing duplicate text: a `blockquote` containing `<p>`s would emit
  both the blockquote and its inner `<p>`. **Recommended rule:** emit only the innermost block —
  collect `find_all(["p","h1"..."h6","li","blockquote"])` but skip any element that has a
  block-level descendant in the set (i.e. emit a `blockquote` as one line only if it has no inner
  `<p>`; otherwise let its inner `<p>`s emit). Document this as the de-dup rule in a comment.

### 3c. Chapter titles

Title resolution per content document, in priority order:
1. The nav/ToC label that points at this document's href (build a `{href -> label}` map from
   `book.toc`, flattening nested `epub.Link` / `(Section, [...])` tuples; strip any `#anchor`).
2. Else the first heading (`h1`–`h6`) text in the document.
3. Else `item.get("title")` / EpubHtml `.title` if set.
4. Else a synthesized fallback: `f"Chapter {order}"` (1-based).

Titles are display-only; they drive the M4B chapter markers downstream.

### 3d. Metadata

- `book.get_metadata("DC", "title")` → `ParsedBook.title` (fallback `path.stem`).
- `book.get_metadata("DC", "creator")` → `ParsedBook.author` (fallback `"Unknown"`).
- Cover: locate via `ITEM_COVER` or the `cover` metadata / `properties="cover-image"`. For v1,
  set `ParsedBook.cover_image_path = None` and treat cover extraction as a later (assemble-stage)
  concern unless trivially available — **do not** write the cover into the workspace from the
  parser (read-only rule). Recommended: leave `None` in v1; note as a follow-up.

### 3e. Return shape

`ParsedBook(title, author, source_path=path, cover_image_path=None, chapters=[ParsedChapter(order, title, lines), ...])`
with `order` 1-based and contiguous in reading order. No empty-text lines, no HTML.

---

## 4. Line / Segment decision (RECOMMENDED — confirm consistency, not a user question)

**Parse emits Lines with NO segments.** `Line.segments` stays `[]` after parse.

Justification:
- The scaffold makes `Segment` the *attributable* unit (narration vs. inline quote) and the
  *render* unit (it carries `audio_cache_key`). Attribution belongs to the `attribute` stage which
  uses `ctx.llm`. Creating a "default narrator segment" in parse would either (a) be immediately
  discarded/re-split by the attribute stage, or (b) tempt the synthesize stage to render
  un-attributed audio. Both are worse than an empty list.
- An empty `segments` list is also a clean, queryable signal that a line is parsed-but-not-yet-
  attributed (`attribute` stage's `is_complete` can check "every line has ≥1 segment").
- `domain/models.py` already defaults `Line.segments = field(default_factory=list)`, so this needs
  no model change.

This is consistent with the per-segment `AudioCache` key: no segment exists ⇒ nothing to key/cache
yet, exactly right at the parse stage.

---

## 5. ParseStage wiring (`src/casttrophizer/pipeline/stages/parse.py`)

### 5a. Parser injection

`StageContext` currently carries `store`, `progress`, `llm`, `tts`, `config` — **no parser**.
Choose the smallest wiring that keeps providers swappable:

**Recommended: select the parser inside `ParseStage` from the source path**, via a tiny module-level
registry, rather than adding a field to `StageContext` (the EPUB parser needs no config and adding
a `StageContext` field touches every stage). Add to `ebook/__init__.py`:

```python
def parser_for(path: Path) -> EbookParser:
    """Return the first registered EbookParser whose .supports(path) is True."""
    for parser in (_EpubParser_singleton, ...):
        if parser.supports(path):
            return parser
    raise EbookParseError(f"no parser supports {path.suffix} files: {path}")
```

`ParseStage.run` calls `parser_for(Path(project.book.source_ebook_path))`. This keeps format
selection behind the `EbookParser` interface and leaves room for future formats without changing
the stage. (Alternative — add `parser: EbookParser | None` to `StageContext` — is also acceptable
if the main agent prefers explicit injection for testability; either is fine. Flagging as a minor
design choice, not a blocker. Recommended: the registry, with the parser still injectable for tests
by allowing `ParseStage(parser=...)` constructor override.)

### 5b. `run` algorithm

```python
def run(self, project: Project, ctx: StageContext) -> StageResult:
    src = Path(project.book.source_ebook_path)
    parser = self._parser or parser_for(src)          # self._parser allows test injection
    ctx.progress.message(f"Parsing {src.name}")

    parsed = parser.parse(src)                          # may raise EbookParseError

    ctx.progress.set_total(len(parsed.chapters))

    chapters: list[Chapter] = []
    for pch in parsed.chapters:
        if ctx.progress.should_stop():
            # partial state: persist what we have, mark not-complete, return STOPPED
            project.book.chapters = chapters
            ctx.store.save(project)
            return StageResult(self.name, ReviewStatus.STOPPED, "stopped during parse")

        chapter_id = new_id("ch")
        lines = [
            Line(id=new_id("line"), chapter_id=chapter_id, order=i, text=text, segments=[])
            for i, text in enumerate(pch.lines)
        ]
        chapters.append(Chapter(id=chapter_id, order=pch.order, title=pch.title, lines=lines))
        ctx.progress.advance(1, message=pch.title)

    # update book metadata in place (preserve source_ebook_path / cover already set)
    project.book.title = parsed.title or project.book.title
    project.book.author = parsed.author or project.book.author
    project.book.chapters = chapters
    if parsed.cover_image_path is not None:
        project.book.cover_image_path = str(parsed.cover_image_path)

    project.stage_status[str(StageName.PARSE)] = ReviewStatus.COMPLETED
    ctx.store.save(project)
    return StageResult(self.name, ReviewStatus.COMPLETED, f"{len(chapters)} chapters")
```

Notes that match existing conventions:
- `stage_status` keys are `str(StageName.X)` — see how `conftest.py` and serialization do it
  (`{k: str(v) ...}`). Be consistent: store with `str(StageName.PARSE)`.
- On `EbookParseError`, let it propagate **or** catch and return
  `StageResult(self.name, ReviewStatus.FAILED, str(exc))`. **Recommended: return FAILED** (the
  runner treats FAILED as halting) and do **not** set `stage_status` — so a fixed input can re-run.
- The stop check is **per chapter** (the natural checkpoint). Partial chapters are saved so a resume
  continues; because `run` rebuilds `chapters` from scratch each call (parse is cheap and
  deterministic), a STOPPED→resume simply re-parses — acceptable for parse. (Synthesize, the
  expensive stage, is where fine-grained skip matters; parse does not need it.)

### 5c. `is_complete`

```python
def is_complete(self, project: Project) -> bool:
    return project.stage_status.get(str(StageName.PARSE)) == ReviewStatus.COMPLETED
```

Mirror the resume model described in `pipeline/stage.py` and `runner.py`. Do **not** also require
`book.chapters` to be non-empty (an empty book could be a legitimately empty EPUB); completion is
purely the recorded status.

---

## 6. Domain / model touchpoints

The current `domain/models.py` is **sufficient** to land this feature — no required changes. Two
**optional** additions for traceability; recommend deferring unless the user wants them now:

- **`Chapter.source_href: str | None`** (the EPUB document href that produced the chapter). Useful
  for "jump to source" debugging and for re-parse diffing later. If added, it must be threaded
  through `serialization.py` (`_chapter_to_dict`/`_chapter_from_dict`) and bump nothing (it is
  additive within schema v1 only if defaulted; cleaner to bump `CURRENT_SCHEMA_VERSION` to 2 and add
  a no-op migration). **Recommendation: skip for v1** to avoid a schema bump for the first feature;
  add when something actually consumes it.
- **`ParsedChapter.source_href`** on the DTO (no schema impact, it is not persisted) — cheap and
  harmless if the coder wants to carry it for the title→href mapping. Optional.

**Decision: no domain model changes required.** Flag to user only if they want source-href
traceability now (would cost a schema-version bump + migration).

---

## 7. Edge cases (the parser/stage must handle, tester must cover)

| Case | Required behavior |
|---|---|
| File missing / not an EPUB / corrupt zip | `EbookParseError` (stage → `FAILED`, status not set). |
| EPUB with **no nav/ToC** | Fall back to per-document heading, then `f"Chapter {n}"`. Order from spine. |
| Image-only / empty chapter (no text blocks) | If a heading exists, emit the heading line only; otherwise emit a `Chapter` with **0 lines** (do not drop the chapter; keeps order stable). Do not crash. |
| Heading-only document | Title from heading; **1 spoken line** (the heading itself, per §2.2). |
| Footnote refs / footnote bodies | Refs stripped from line text; bodies dropped (§2.3). |
| Nested `blockquote`/`<p>` | De-dup rule (§3b) — emit innermost block only, no duplicated text. |
| Encoding | Let `ebooklib`/`bs4` decode per the document's declared charset; never assume cp1252. Lines are `str`. Assert no `UnicodeDecodeError` escapes (wrap in `EbookParseError`). |
| Very long chapter (thousands of `<p>`) | No special handling needed for parse (cheap); just ensure progress is per-chapter, not per-line, so the UI updates. |
| Whitespace-only / `&nbsp;` paragraphs | Collapsed → empty → skipped. |
| Duplicate titles across chapters | Allowed; titles are display-only, order disambiguates. |

---

## 8. Test plan (for the tester — all offline, no network/models)

Extend `tests/data/make_sample_epub.py` and add `tests/pipeline/test_parse_stage.py` +
`tests/ebook/test_epub_parser.py`.

### 8a. Fixture extensions (`make_sample_epub.py`)
The current fixture is good for the happy path but should grow optional builders so edge cases are
covered without one giant EPUB. Add small factory functions (keep `make_sample_epub` as-is for
back-compat with existing fixtures):
- `make_epub_no_toc(out_path)` — spine documents, headings present, **no** `EpubNav`/`toc`.
- `make_epub_image_only_chapter(out_path)` — one chapter whose body is just `<p><img/></p>`.
- `make_epub_with_footnote(out_path)` — a `<p>` with a `noteref` anchor + an `aside epub:type="footnote"`.
- `make_epub_blockquote(out_path)` — a `<blockquote><p>...</p></blockquote>` to exercise de-dup.
- (Optional) `make_epub_frontmatter(out_path)` — a title-page spine item before chapters.

### 8b. `EpubParser` unit tests (`tests/ebook/test_epub_parser.py`)
Parse the generated EPUBs directly (no stage) and assert on `ParsedBook`:
- **Metadata:** `title == "A Sample Tale"`, `author == "Test Author"`.
- **Chapter count & order:** 2 chapters; `chapters[0].title == "Chapter One"`,
  `chapters[1].title == "Chapter Two"`; `order` is `[1, 2]`.
- **Line splitting:** chapter one yields the heading line first (document order), then the 3 body
  `<p>` texts in order (`"Chapter One"`, `"The narrator set the scene."`, `'"Hello there," said Alice.'`,
  `'"And hello to you," Bob replied.'`); the `<h1>Chapter One</h1>` heading **is** emitted as a line
  (per §2.2, narrate-headings) — first here because it leads the document — and also equals
  `chapter.title`. No empty strings, no HTML tags in any line.
- **No-ToC EPUB:** titles fall back to headings / `Chapter N`; reading order preserved.
- **Image-only chapter (no heading):** chapter present, `lines == []`.
- **Footnote:** ref marker absent from line text; footnote body text absent from lines.
- **Blockquote de-dup:** the quoted text appears in exactly one line, not two.
- **Bad input:** `parse(missing.epub)` and `parse(not_an_epub.txt)` raise `EbookParseError`.

### 8c. `ParseStage` integration tests (`tests/pipeline/test_parse_stage.py`)
Build a `Project` whose `book.source_ebook_path` is `sample_epub` with empty chapters (or add a
`parse_ready_project` fixture in `conftest.py`), run the stage with `tmp_workspace` and a
`RecordingProgressReporter`, then assert against the **persisted** project (reload via
`tmp_workspace.load()` — proves it was saved, not just mutated in memory):
- Returns `StageResult(stage=PARSE, status=COMPLETED)`.
- `stage_status[str(StageName.PARSE)] == ReviewStatus.COMPLETED`.
- Reloaded `book.chapters` has the expected count/titles/order; every `Line.text` non-empty;
  **every `Line.segments == []`** (the §4 decision — assert explicitly).
- `book.title`/`author` populated from the EPUB.
- **Resume / idempotence:** `stage.is_complete(project)` is `True` after run; calling
  `Pipeline.next_stage(project)` returns the *next* stage (`CorrectTextStage`), not parse; running
  the pipeline again does **not** re-run parse (assert via a spy/counter on the injected parser, or
  assert chapter ids are stable when `is_complete` short-circuits).
- **Stop mid-parse:** set `recording_progress.should_stop` to trip after the first chapter; assert
  result is `STOPPED`, `stage_status[PARSE]` is **not** `COMPLETED`, and a subsequent run with
  stop cleared completes and `is_complete` becomes `True`.
- **Read-only input:** assert the source `.epub` file bytes/mtime are unchanged after parsing, and
  nothing was written outside `tmp_workspace` (the parser must not write into the workspace either).
- **Failure path:** point `source_ebook_path` at a non-EPUB; assert `FAILED` result and
  `stage_status[PARSE]` is unset (so a fixed input can re-run).
- **Progress:** `recording_progress` recorded `set_total(n_chapters)` and one `advance` per chapter.

All tests import only the fakes already in `tests/fakes/`; the parse stage needs neither `llm` nor
`tts`, so `StageContext(store=..., progress=...)` with both `None` is the call shape.

---

## 9. Ordered task list (coder)

1. **EpubParser** (`src/casttrophizer/ebook/epub.py`) — implement `parse` per §3 (spine order,
   HTML→text, heading/ToC titles, footnote stripping, blockquote de-dup, metadata). Lazy imports.
   Raise `EbookParseError` on failure.
2. **Parser registry** (`src/casttrophizer/ebook/__init__.py`) — add `parser_for(path)` per §5a
   (or, if the main agent chose `StageContext` injection, add the field instead — confirm first).
3. **ParseStage** (`src/casttrophizer/pipeline/stages/parse.py`) — implement `is_complete` (§5c)
   and `run` (§5b): map `ParsedBook` → `project.book`, segments empty (§4), progress per chapter,
   `should_stop` checkpoint, persist via `ctx.store.save`, set `stage_status[PARSE]=COMPLETED`,
   FAILED on parse error. Allow optional `parser=` constructor arg for test injection.
4. **Fixture builders** (`tests/data/make_sample_epub.py`) — add the edge-case EPUB factories (§8a).
5. **Tests** — `tests/ebook/test_epub_parser.py` (§8b) and `tests/pipeline/test_parse_stage.py`
   (§8c), plus a `parse_ready_project` fixture in `conftest.py` if helpful.
6. **Tooling** — `ruff check`, `black --check`, `mypy src`, `pytest` green.

Dependency order: 1 → 2 → 3 (stage uses parser); 4 before 5.

---

## 10. Risks

- **`ebooklib` spine API surface.** `book.spine` entries and nav-flattening (`book.toc` nesting of
  `Link`/`Section`/tuples) are the fiddliest part; the coder should verify against the actually
  installed `ebooklib` version, not from memory. The generated fixtures make this verifiable offline.
- **Blockquote/nested-block de-dup** is the most likely source of duplicated-line bugs — the
  dedicated `make_epub_blockquote` test exists to pin it.
- **Cover extraction deferred** — leaving `cover_image_path = None` means the assemble stage will
  later need its own cover-resolution path. Acceptable for this feature; note it as a follow-up.
- **Scope creep guard:** do NOT implement segmentation, text correction, or cover embedding here.
  Parse stops at populated `Line`s with empty `segments`.

---

## 11. Verification strategy (what the tester must prove)

1. `EpubParser.parse` returns correct metadata, chapter count/titles/order, and paragraph-level
   lines with no HTML and no empty strings — across the happy-path and each edge-case EPUB.
2. `ParseStage.run` persists the tree (proven by reload), sets `stage_status[PARSE]=COMPLETED`,
   and leaves `Line.segments == []`.
3. Resume: `is_complete` true after success; re-run does not re-parse; `next_stage` advances.
4. Stop: STOPPED mid-parse leaves status not-completed; later run completes.
5. Read-only inputs: source EPUB unchanged; nothing written outside the workspace.
6. Failure: bad input → FAILED, status unset, re-runnable.
7. Fully offline — no LLM/TTS/network; `StageContext.llm`/`tts` are `None`.
8. `ruff` / `black` / `mypy src` / `pytest` all green.

---

## Handoff

**User decisions (CONFIRMED):** (1) **paragraph-level** lines, not sentence-level; (2) **headings
are narrated** — the heading is emitted as the chapter's first `Line` (narrator) AND used as the
chapter title/M4B marker. The coder builds to these.

**Coder, build Task 1 first:** implement `EpubParser.parse` in
`src/casttrophizer/ebook/epub.py` per §3 (spine-ordered documents, BeautifulSoup HTML→text,
ToC/heading titles, footnote stripping, blockquote de-dup, lazy imports, `EbookParseError` on
failure), returning a `ParsedBook` of paragraph-level `ParsedChapter.lines`. Verify against the
generated fixtures before wiring `ParseStage`.
