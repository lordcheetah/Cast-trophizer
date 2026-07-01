# Plan: Assemble Stage (`AssembleStage`) — the final pipeline stage

**Goal:** Produce `<workspace>/output/<book>.m4b` — every renderable per-segment WAV the synthesize
stage cached, concatenated in reading order (chapter → line → segment), AAC-encoded into an M4B
container, with **chapter markers** (title + start time per `Chapter`) and an **embedded cover image**
when one is available, treating the source ebook / voice clips / cover as read-only inputs.

Pipeline stage: **`assemble`** (sixth/final stage; after `synthesize`). It reads the segment WAVs from
the workspace `AudioCache`, computes per-chapter durations, and shells the concatenation + M4B
encode + chapter/cover/tag write out to `M4BAssembler` (ffmpeg + mutagen). It is the first stage that
touches an **external binary** (`ffmpeg`, not a pip dep). Like every stage it is Qt-free, resumable,
and records `stage_status[str(StageName.ASSEMBLE)] = COMPLETED`.

This plan mirrors the confirmed patterns from `synthesize-stage.md` and `review-stage.md`: a
fail-fast precheck, a thin stage over an offline orchestration helper, dependency injection of the
external-tool boundary (`assembler=None` → default `M4BAssembler`), and `# VERIFY:` comments on every
real ffmpeg/ffprobe/mutagen call the coder cannot verify offline.

---

## 0. Decisions locked by existing code (do not relitigate)

- **`M4BAssembler` + DTOs already exist** in `src/casttrophizer/audio/assembler.py`:
  - `ChapterMarker(title: str, start_s: float, end_s: float)`.
  - `AssemblyRequest(segment_audio_paths: list[Path], chapters: list[ChapterMarker], out_path: Path,
    title: str, author: str, cover_image_path: Path | None = None, metadata: dict[str, str] = {})`.
  - `M4BAssembler.__init__(ffmpeg_path: str = "ffmpeg")`, `is_available() -> bool` (already
    `shutil.which(self._ffmpeg_path) is not None` — cheap, no subprocess), `assemble(request) ->
    Path` (stub `raise NotImplementedError`). **Fill `assemble`; keep the signatures.** The plan uses
    the *request object* form (`assemble(request)`), not a positional-args form — match the stub.
- **`AssemblyError`** already exists in `errors.py` ("The final audiobook could not be assembled
  (e.g. ffmpeg failure)"). Use it for ffmpeg/probe/mutagen failures — do **not** invent a new error.
- **`WorkspaceLayout.output_dir`** = `<root>/output` and **`ensure_dirs()`** already creates it. The
  M4B lands at `output/<sanitized-title>.m4b`. There is currently **no** `output_path(name)` helper —
  the stage derives the filename (see §7). Adding a small `layout.output_path(filename)` helper is
  optional and non-breaking.
- **`AudioCache.path_for_key(key)`** → `<root>/audio/<key>.wav` and **`has(key)`** already exist and
  are the only way to resolve a segment's WAV. Reuse verbatim; do not recompute paths.
- **`Segment.audio_cache_key: str | None` / `audio_status: ReviewStatus`** already exist and
  round-trip. Synthesize stamps `audio_cache_key` + `audio_status=COMPLETED` on a rendered segment;
  a whitespace-only segment is left with **no** `audio_cache_key` and `audio_status=PENDING`
  (`synthesize_chapter` `advance(None); continue` before stamping). A `FAILED` segment has
  `audio_cache_key=None` (synthesize clears it on failure).
- **`Book.cover_image_path: str | None`** exists on the model but **the parse stage sets it to
  `None`** (EPUB cover extraction was deferred). So in the common case it is `None` at assemble time.
- **`SynthesisResult.duration` is discarded** by synthesize — durations are **not** persisted
  anywhere. Assemble must obtain per-segment durations itself (§3).
- **Stage contract** (`pipeline/stage.py`): poll `ctx.progress.should_stop()` at safe checkpoints,
  return STOPPED with partial state persisted, idempotent/resumable, Qt-free, `is_complete` purely
  `stage_status`-driven (`project.stage_status.get(str(StageName.X)) == COMPLETED`). Runner
  (`runner.py`) halts on STOPPED / NEEDS_REVIEW / FAILED (`_HALTING`). `stage_status` keys are
  `str(StageName.X)`.
- **`StageContext`** carries `tts`/`llm`/`config`; assemble needs **none** of them — it needs only
  `ctx.store` and `ctx.progress`. The `M4BAssembler` is **not** a provider on `StageContext`; it is
  injected via `AssembleStage.__init__(assembler=None)` (test seam), defaulting to a real
  `M4BAssembler()` — mirroring how synthesize gets its provider, but from the constructor because the
  assembler is stage-local, not a swappable provider on the context.
- **`CURRENT_SCHEMA_VERSION == 1`.** No field on any model needs to change for the recommended plan
  (§8). No bump.
- **Lazy-import discipline:** `audio/assembler.py` must import `mutagen` **inside** methods only, and
  never import `torch`/`chatterbox`/`PySide6`/`anthropic`. `audio/assemble.py` (new orchestration) and
  `pipeline/stages/assemble.py` must import no heavy dep. New lazy-import cases are required (§10).

---

## 1. Where this sits & what it reads/writes

| | |
|---|---|
| **Reads** | `project.book.chapters[*].lines[*].segments[*]` (`.text`, `.audio_cache_key`, `.audio_status`); `project.book.title` / `.author` / `.cover_image_path`; the per-segment WAVs at `AudioCache.path_for_key(segment.audio_cache_key)`. |
| **Writes** | `<workspace>/output/<title>.m4b` (the only durable artifact). Intermediate build files under a workspace build dir (§7), deleted on success. **Nothing on the `Project` object mutates except** `stage_status[ASSEMBLE]`. |
| **Uses** | `M4BAssembler` (ffmpeg + mutagen) — the only external-tool call. Ordering, WAV resolution, duration probing, and chapter-marker computation are done in the offline orchestration; the assembler is handed a fully-computed `AssemblyRequest`. |
| **Persists** | whole `Project` via `ctx.store.save` (only `stage_status[ASSEMBLE]` changes). |
| **Records** | `stage_status[str(StageName.ASSEMBLE)] = COMPLETED` on success. |
| **Progress** | per **chapter** for the build + a final "encoding" step (assembly is monolithic at the ffmpeg boundary; see §6 for stop granularity). |

The stage does **not** call any TTS/LLM provider and needs no `ctx.tts`/`ctx.llm`.

---

## 2. Precondition / input readiness — FLAG (recommend FAIL-fast)

Assemble needs every **renderable** segment to have a rendered WAV on disk. Mirror synthesize's
`unresolved_voices` precheck with a new `unrendered_segments(project, cache) -> list[str]`.

### 2a. "Renderable" segment — must match synthesize exactly

A segment is **renderable** iff `segment.text.strip()` is non-empty. This is the **identical**
whitespace-skip rule `synthesize_chapter` uses (`if not segment.text.strip(): advance(None);
continue`). Assemble MUST skip the same whitespace-only segments — they have no WAV, and including
them would desync the concat / chapter timing. **Reuse the rule; do not invent a different one.**
(Recommendation: expose a tiny shared predicate `is_renderable(segment) -> bool` used by both
`audio/synthesize.py` and `audio/assemble.py` so they can never drift. Optional but cheap — FLAG.)

### 2b. The precheck

`unrendered_segments(project, cache)` scans every **renderable** segment and collects a short human
identifier (e.g. `"<chapter title> / seg <id>"`) for any segment where **any** of:

1. `segment.audio_cache_key is None` (never rendered), **or**
2. `segment.audio_status not in {COMPLETED, APPROVED}` (i.e. `PENDING`/`FAILED`/`NEEDS_REVIEW`), **or**
3. `not cache.has(segment.audio_cache_key)` (key stamped but WAV missing on disk — crash/GC/deletion).

Reuse `RENDERED_STATUSES` from `audio/synthesize.py` (`frozenset({COMPLETED, APPROVED})`) for check 2
so "rendered" means the same thing in both stages.

If the list is non-empty → `StageResult(ASSEMBLE, FAILED, "N segments not rendered — run synthesize
first: …")`, **status left unset** (re-runnable once synthesize completes), **nothing produced**.
Run this **before** any ffmpeg work so a half-synthesized project fails instantly.

**Recommendation: FAIL-fast (do not silence-substitute).** A book with silent gaps where segments
should be is a corrupt deliverable presented as success; the user should run synthesize first. The
alternative (substitute a silence clip for each unrendered segment and proceed) is worse — it hides a
real gap and the chapter timing/attribution would silently mislead. **FLAG (DECISION #3).**

> Cross-stage note: synthesize already guarantees every renderable segment is `COMPLETED` on a clean
> COMPLETED run, so on the normal path this precheck passes trivially. It exists to catch (a) running
> assemble before synthesize, (b) a resume where synthesize STOPPED partway, (c) a `FAILED` segment
> from flag-and-continue, and (d) a WAV deleted from the cache after synthesize.

---

## 3. Segment ordering, duration & chapter-marker computation (the tricky part)

All of this is **offline** (no ffmpeg) and lives in `audio/assemble.py` so it is unit-testable with
the tiny silent WAVs the fakes already produce.

### 3a. Ordering — the exact stitch order

Iterate **chapters in list order** (`project.book.chapters`), then **lines in list order**
(`chapter.lines`), then **segments in list order** (`line.segments`), skipping whitespace-only
segments (§2a). For each kept segment, resolve its WAV path via
`cache.path_for_key(segment.audio_cache_key)`. This yields the flat `segment_audio_paths` list in
playback order.

> The domain already stores `Chapter.order` / `Line.order`, but the **list order** in the parsed tree
> is authoritative and is what every other stage iterates (synthesize iterates `chapters`/`lines`
> directly). **Recommend: use list order** (consistent with synthesize) and NOT re-sort by `.order`.
> If `.order` and list order can diverge, that is a parse-stage invariant, not assemble's to fix.
> FLAG (minor): confirm list order is authoritative.

### 3b. Duration source — FLAG (recommend stdlib `wave`)

Chapter start times are cumulative audio duration, and durations are **not persisted**. Options:

- **(A) stdlib `wave`** — open each `<key>.wav`, `duration = wav.getnframes() / wav.getframerate()`.
  Dependency-free, exact for PCM WAV (which is what Chatterbox/`FakeTTSProvider` write), no subprocess,
  works on the tiny fixture WAVs in CI. **Recommended.**
- **(B) `ffprobe`** — accurate for any container but adds a second external binary, a subprocess per
  segment (slow for thousands of segments), and is **unavailable in CI** (so tests would have to mock
  it). Only needed if segment audio might not be plain PCM WAV — it isn't (synthesize writes WAV).
- **(C) sum durations reported by ffmpeg during concat** — couples timing to the concat step, harder
  to compute *before* building the metadata file, and still needs a probe for the chapter boundaries.

**Recommendation: (A) stdlib `wave`.** It is exact for the PCM WAVs we produce, offline-testable on
the fixture WAVs, and needs no second binary. Add a small helper `wav_duration_s(path: Path) -> float`
in `audio/assemble.py` (open in `"rb"`, read frames/rate, close). Guard a malformed/short WAV by
raising `AssemblyError` (a WAV that fails to open means a corrupt cache → fail loudly, don't guess 0).
**FLAG (DECISION #4).**

### 3c. Chapter markers

Walk chapters in order, accumulating a running `cursor_s` (float seconds). For each chapter:

```
start_s = cursor_s
chapter_duration = sum(wav_duration_s(path) for path in this chapter's kept segment WAVs)
                   + inter-segment / inter-line gaps within the chapter (§3d)
end_s = start_s + chapter_duration
cursor_s = end_s + inter_chapter_gap   # the gap is played *between* chapters (§3d)
```

Emit `ChapterMarker(title=chapter.title, start_s=start_s, end_s=end_s)` for **every** chapter,
including one with zero kept segments (§9 — a zero-length marker at the current cursor, so chapter
count stays 1:1 with the book and the user can still navigate to it). Start times are therefore
**monotonically non-decreasing** and computed purely from durations + gaps — the whole computation is
offline and deterministic.

> **Timing must match what ffmpeg actually produces.** Whatever gaps §3d injects into the concatenated
> audio must be the **same** gaps folded into these start/end times, or the chapter markers will drift
> from the audio. Keep one source of truth: the gap constants live in `audio/assemble.py`, feed both
> the marker math and the silence-padding inserted into the concat (§3d). AAC encoding can shift exact
> sample boundaries slightly; markers are "good enough for navigation," not sample-exact — acceptable.

### 3d. Silence / padding between units — FLAG (recommend small defaults)

Chatterbox renders individual segments with no built-in inter-segment pause, so back-to-back
concatenation sounds rushed. Recommended defaults (constants in `audio/assemble.py`):

- **inter-segment gap: 0.0s** (segments within a line are one continuous utterance split only by
  attribution — a pause there sounds unnatural). Recommend none.
- **inter-line gap: ~0.4s** (a brief breath between paragraphs/quote runs).
- **inter-chapter gap: ~1.0s** (a longer beat between chapters; the chapter marker also delineates it).

Implementation of the gap in the concat (§4): the cleanest tool-agnostic approach is to generate one
short silent WAV per distinct gap length once (via `wave`, same sample rate/format as the segment
WAVs), cache it in the build dir, and insert it into the concat list at the right boundaries — so the
concat demuxer sees a homogeneous list of WAVs and no `filter_complex` is needed. The **same** gap
durations feed §3c's marker math. Make the three gap constants module-level so they are trivially
overridable; **v1: not user-configurable** (no config field, no schema change).

**FLAG (DECISION #5):** confirm the three defaults (0.0 / 0.4 / 1.0s) and that they are fixed
constants for v1 (not surfaced in `AppConfig`/UI yet).

---

## 4. Concatenation & M4B encode (inside `M4BAssembler.assemble`) — all `# VERIFY:`

The assembler receives a fully-computed `AssemblyRequest` (ordered WAV paths incl. any silence-gap
WAVs the orchestration inserted, chapter markers, out_path, title/author, optional cover) and does the
external work. **The coder cannot run ffmpeg in CI and cannot web-verify flags — every real command
string gets a `# VERIFY:` comment for the user to confirm at runtime.**

### 4a. Concat approach — concat demuxer with a file list (NOT command-line args)

**Recommend the ffmpeg concat *demuxer*** driven by a **text file list** written to the build dir:

```
# concat_list.txt  (one line per WAV, ffmpeg concat-demuxer format)
file '/abs/path/audio/<key1>.wav'
file '/abs/path/silence_0400.wav'
file '/abs/path/audio/<key2>.wav'
...
```

Then a single ffmpeg invocation reads the list, encodes to AAC in an M4B/MP4 container:

```python
# VERIFY: exact ffmpeg concat-demuxer + AAC/M4B flags must be confirmed at runtime.
#   Approx:  ffmpeg -f concat -safe 0 -i concat_list.txt -c:a aac -b:a <bitrate> -movflags +faststart <out>.m4b
#   - concat demuxer (NOT the concat *filter* and NOT -i with a huge arg list) so a book with
#     thousands of segments stays a small file list, never a giant command line (§9).
#   - all inputs are same-sample-rate PCM WAV (Chatterbox is fixed-sr); if a WAV's rate differs,
#     concat-demuxer can glitch — the orchestration should normalize/guard (§9). VERIFY whether an
#     explicit -ar/-ac normalize pass is needed.
#   - container: .m4b is an MP4/AAC container; VERIFY the codec/bitrate the user wants.
```

Rationale for the demuxer over `filter_complex`: (a) no per-input filter graph that blows up at
thousands of inputs; (b) a file list sidesteps OS command-line length limits (Windows ~32k chars) —
critical for a long book (§9); (c) simplest to reason about and to point a `# VERIFY:` at.

**Escaping:** the concat file format requires single-quoting paths and escaping embedded quotes
(`'\''`). Write paths as **absolute** (they are, from `cache.path_for_key`). `# VERIFY:` the exact
escaping rule for paths containing quotes/unicode on Windows.

### 4b. Chapters vs cover vs tags — who owns what

Two viable splits (**FLAG, recommend the ffmpeg-chapters split**):

- **Recommended: ffmpeg writes chapters + basic tags during the encode; mutagen embeds the cover (and
  fixes/normalizes tags) after.** ffmpeg reads a chapter/metadata file (`-i` a second
  `ffmetadata`-format file, or `-map_metadata`) containing `[CHAPTER]` blocks; mutagen (`MP4` +
  `MP4Cover`) opens the finished `.m4b` and writes the cover atom + title/author/album tags. This
  keeps binary image handling in mutagen (robust, pip dep) and timeline handling in ffmpeg.
- Alternative: ffmpeg does concat/encode only; **mutagen writes chapters *and* cover** via MP4 chapter
  atoms. mutagen's chapter support is more fiddly than its tag/cover support — prefer ffmpeg for
  chapters.

Chapter metadata file (ffmetadata format), one block per marker:

```
;FFMETADATA1
title=<book title>
artist=<author>

[CHAPTER]
# VERIFY: ffmetadata chapter block format + TIMEBASE.
TIMEBASE=1/1000
START=<start_ms>
END=<end_ms>
title=<chapter title, ESCAPED>
```

```python
# VERIFY: the ffmetadata chapter syntax (TIMEBASE, START/END units = ms here), how it is passed
#   (second -i input + -map_metadata / -map_chapters), and that chapter titles are escaped per
#   ffmetadata rules ('=', ';', '#', '\\', newline must be backslash-escaped).
```

**Escaping (chapter titles):** ffmetadata requires escaping `=`, `;`, `#`, `\`, and newlines with a
backslash. A title like `Chapter 1: "The End" #2` or a unicode `Rësumé` must be escaped before it
lands in the metadata file (§9). Provide a `_escape_ffmetadata(text: str) -> str` helper; `# VERIFY:`
the exact character set against ffmpeg docs at runtime.

### 4c. Cover embedding (mutagen)

```python
# VERIFY: mutagen MP4 cover-atom API. Approx:
#   from mutagen.mp4 import MP4, MP4Cover
#   mp4 = MP4(str(out_path))
#   data = Path(cover).read_bytes()
#   fmt = MP4Cover.FORMAT_PNG if <png> else MP4Cover.FORMAT_JPEG   # VERIFY format detection
#   mp4["covr"] = [MP4Cover(data, imageformat=fmt)]
#   mp4["\xa9nam"] = title; mp4["\xa9ART"] = author; mp4["\xa9alb"] = title   # VERIFY tag atoms
#   mp4.save()
```

Import `mutagen` **inside** `assemble` (lazy — the lazy-import test forbids it at module top). Only
embed the cover when `request.cover_image_path` is set **and** the file exists; otherwise write tags
only and proceed (§5). If the cover file is an unsupported/wrong format, `# VERIFY:` behavior — either
detect by extension/magic and skip with a logged warning, or let mutagen raise → wrap in
`AssemblyError`. **Recommend: skip a bad cover with a warning, still produce the M4B** (a bad cover
should not fail the whole render) — FLAG (part of DECISION #1).

### 4d. Failure & cleanup

Any non-zero ffmpeg exit, a probe failure, or a mutagen error → raise `AssemblyError(<detail incl.
ffmpeg stderr tail>)`. The stage catches it and returns `FAILED` (§6). On failure, do **not** leave a
half-written `.m4b` at `out_path` — write to a temp path in the build dir and only move it into
`output/` on full success (mirrors the store's atomic-write ethos). Clean up the build dir on both
success and failure (best-effort).

---

## 5. Cover art — FLAG (recommend optional-embed-if-set)

`Book.cover_image_path` exists but parse leaves it `None`. Three policies:

- **(a) Require a user-supplied image** — assemble FAILs if no cover. Too strict; blocks a valid
  audiobook over a nice-to-have.
- **(b) Extract the EPUB cover now** — touches the parse stage (or a new step) to pull the cover out of
  the EPUB into the workspace and set `cover_image_path`. Real work, cross-stage, out of scope here.
- **(c) Optional embed-if-set** — assemble embeds a cover **iff** `book.cover_image_path` is set and
  the file exists; otherwise it proceeds and produces an M4B with **no** cover. No failure, no
  cross-stage change.

**Recommendation: (c) optional-embed.** This stage stops at "embed the cover if we have one." EPUB
cover extraction (parse-stage change) and a UI cover-picker (writes `cover_image_path` like
`register_voice_clip` writes voice paths) are **separate future work** — call them out as the gap.
The cover file is a **read-only input** (referenced, never copied/mutated), exactly like voice clips.
**FLAG (DECISION #1).**

> When (b)/UI-picker later lands, nothing in assemble changes — it already embeds whatever
> `cover_image_path` points at. That is the whole point of (c).

---

## 6. Stage wiring & resumability (`pipeline/stages/assemble.py`)

Mirror `SynthesizeStage.run`: precheck → build request offline → hand to the assembler → persist →
record COMPLETED. The stage stays thin; the offline orchestration (ordering, durations, markers, gap
WAVs, request assembly) lives in `audio/assemble.py`.

```python
class AssembleStage(Stage):
    name = StageName.ASSEMBLE

    def __init__(self, assembler: M4BAssembler | None = None) -> None:
        self._assembler = assembler or M4BAssembler()   # default real; tests inject a fake

    def is_complete(self, project: Project) -> bool:
        # Status-driven like every other stage.
        return project.stage_status.get(str(StageName.ASSEMBLE)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        if not self._assembler.is_available():
            return StageResult(self.name, ReviewStatus.FAILED,
                               "ffmpeg not found on PATH — install ffmpeg to assemble the M4B")

        cache = AudioCache(ctx.store.layout)

        unrendered = unrendered_segments(project, cache)          # §2 precheck
        if unrendered:
            return StageResult(self.name, ReviewStatus.FAILED,
                               f"{len(unrendered)} segments not rendered — run synthesize first: "
                               f"{', '.join(unrendered[:5])}"
                               + (" …" if len(unrendered) > 5 else ""))

        # Offline: ordering + durations + markers + gap WAVs -> a fully-computed AssemblyRequest.
        request, chapter_count = build_assembly_request(
            project, cache, ctx.store.layout,
            progress=ctx.progress, should_stop=ctx.progress.should_stop,
        )
        if request is None:                                       # stop requested during build
            return StageResult(self.name, ReviewStatus.STOPPED, "stopped during assemble")
        if not request.segment_audio_paths:                       # nothing renderable at all (§9)
            return StageResult(self.name, ReviewStatus.FAILED, "no rendered audio to assemble")

        ctx.progress.message("encoding M4B")
        try:
            out = self._assembler.assemble(request)               # the ffmpeg/mutagen boundary
        except AssemblyError as exc:
            return StageResult(self.name, ReviewStatus.FAILED, str(exc))

        project.stage_status[str(StageName.ASSEMBLE)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(self.name, ReviewStatus.COMPLETED, f"assembled {out.name}")
```

`build_assembly_request(project, cache, layout, *, progress, should_stop) -> tuple[AssemblyRequest |
None, int]` (in `audio/assemble.py`):

- `ctx.progress.set_total(len(project.book.chapters))`; poll `should_stop()` once per chapter while
  computing durations (the build is cheap — `wave` header reads — but a huge book still benefits), and
  return `(None, 0)` if stopped.
- Build the ordered `segment_audio_paths` (§3a), computing chapter markers (§3c) and inserting
  gap-WAV paths (§3d) as it goes; `progress.advance(1)` per chapter.
- Derive `out_path = layout.output_dir / sanitize_filename(project.book.title) + ".m4b"` (§7).
- Resolve the cover: `cover = Path(book.cover_image_path)` iff set and `is_file()`, else `None` (§5).
- Return `AssemblyRequest(segment_audio_paths, chapters=markers, out_path=..., title=book.title,
  author=book.author, cover_image_path=cover, metadata={...})`.

### 6a. Stop granularity
Assemble is **monolithic at the ffmpeg boundary** — the encode is a single subprocess that cannot be
cooperatively stopped mid-way. So `should_stop()` is polled during the **offline build** (per chapter)
and once **before** launching ffmpeg; once ffmpeg starts, the stage runs it to completion (or lets a
future cancel kill the subprocess — out of scope). This is acceptable: the build is fast; the encode
is the slow part and is atomic. **Recommend: poll during build + before encode; no mid-encode stop.**
FLAG (minor) if the user wants the ffmpeg subprocess killable on stop (needs process-group handling —
defer).

### 6b. Idempotency / re-run — FLAG (recommend skip-if-up-to-date via stage_status)
`is_complete` is purely `stage_status[ASSEMBLE] == COMPLETED`. Two behaviors on an explicit re-run of
`run` (e.g. the user re-runs assemble directly, not through `Pipeline` which would skip it):

- **(A) Always rebuild** the M4B unconditionally.
- **(B) Skip if up-to-date:** if `stage_status[ASSEMBLE] == COMPLETED` **and** the output file exists,
  return COMPLETED without re-encoding.

Through the `Pipeline`, `is_complete` already prevents a re-run (the runner skips completed stages), so
this only matters for a direct `run` call. **Recommend (A) always rebuild when `run` is called
directly** (simplest, and the caller asked for it) — the `Pipeline` path is already idempotent via
`is_complete`. Note: assemble does **not** get free per-segment caching like synthesize (the M4B is one
monolithic artifact); the cheap-to-recompute part is only the offline build. Intermediate per-chapter
audio is **not** cached across runs in v1 (§7) — a re-run rebuilds the single M4B from the (already
cached) segment WAVs. **FLAG (DECISION #6).**

### 6c. What "complete" means
Output M4B exists at `output/<title>.m4b` **and** `stage_status[ASSEMBLE] == COMPLETED`. `run` writes
the flag only after `assemble` returns successfully, so the flag can't be COMPLETED without the file
(barring the user deleting the file afterward — acceptable, same as every other stage's flag honesty).

---

## 7. Workspace / layout & cleanup

- **Output:** `output/<sanitize_filename(book.title)>.m4b`. `ensure_dirs()` already creates
  `output/`. A `sanitize_filename(title: str) -> str` helper (in `audio/assemble.py`) strips/replaces
  filesystem-hostile chars (`/ \ : * ? " < > |`, control chars), collapses whitespace, trims, and
  falls back to `"audiobook"` when the title is empty/all-stripped (§9). Preserve unicode letters
  (an `ë`/CJK title is a valid filename on modern FS) — only strip the reserved set. **FLAG (minor):**
  confirm the sanitize rule + fallback name.
- **Intermediate build files** (concat_list.txt, silence WAVs, the ffmetadata chapter file, the
  temp `.m4b` before atomic move) go in a **build dir under the workspace**, e.g.
  `<root>/output/.build/` (or `tempfile.mkdtemp(dir=layout.output_dir)`). Deleted best-effort on
  success and on failure. Nothing is written outside the workspace.
- **Source ebook, voice clips, and cover image stay read-only** — the cover is opened for reading only
  and never copied into the workspace (consistent with voice clips). Segment WAVs in `audio/` are read
  only (never mutated) by assemble.
- **Optional layout helper:** add `WorkspaceLayout.output_path(filename: str) -> root/output/filename`
  and/or a `build_dir` property — non-breaking, keeps path logic in `layout.py`. Optional; the stage
  can compose paths itself. FLAG (minor).

---

## 8. Schema impact — NONE required (recommended plan)

Every field assemble reads (`Segment.audio_cache_key`/`audio_status`, `Book.title`/`author`/
`cover_image_path`, chapters/lines/segments) already exists and round-trips. Assemble writes only
`stage_status[ASSEMBLE]` (already a dict field). **No `schema_version` bump, no migration.**
`CURRENT_SCHEMA_VERSION` stays **1**.

A bump would be forced only by out-of-scope choices, all deferred/flagged:
- persisting per-segment `duration_s`/`sample_rate` on `Segment` (to skip probing) — **defer**;
  `wave` probing is cheap and exact (§3b).
- a project-level `output_path` field — **defer**; derived from title (§7).
- a config-backed padding/bitrate field — **defer**; v1 uses module constants (§3d, §4a).

---

## 9. Edge cases (stage must handle; tester must cover)

| Case | Required behavior |
|---|---|
| Chapter with **zero renderable** segments (empty/heading-only) | Emit a `ChapterMarker` with `start_s == end_s` at the current cursor (zero-length), no audio contributed. Chapter count stays 1:1 with the book; navigation still lands there. (Alt: skip the marker — recommend **keep** so markers match the book's chapter list. FLAG minor.) |
| **Whole book** with zero renderable segments | `build_assembly_request` returns an empty `segment_audio_paths` → stage returns FAILED ("no rendered audio to assemble"). Do not invoke ffmpeg on an empty list. |
| **Single-chapter** book | One marker `start=0, end=total`; normal path. |
| **Very long** book (thousands of segments) | Concat **demuxer + file list** (§4a), never a command-line arg list — avoids OS arg-length limits. `wave` duration reads are O(n) header reads, fine. |
| **Missing / unicode / empty** book title | `sanitize_filename` strips reserved chars, preserves unicode letters, falls back to `"audiobook"` if empty (§7). |
| **Cover wrong format / missing / bad** | Missing/unset → no cover, proceed (§5). Wrong format → skip cover with a warning, still produce M4B (recommend) or `AssemblyError` (FLAG, DECISION #1). |
| **ffmpeg not installed** | `assembler.is_available()` False (cheap `shutil.which`) → FAILED with a clear "install ffmpeg" message, status unset, nothing produced. |
| **WAV sample-rate mismatch** | Chatterbox is fixed-sr, but guard: `# VERIFY:` whether concat-demuxer needs an explicit `-ar/-ac` normalize; the orchestration can also detect divergent rates via `wave` and either normalize (re-encode) or `# VERIFY:` fail. Recommend detect-and-`# VERIFY:` (don't silently produce a glitchy concat). |
| **Chapter title breaks ffmetadata** (`=`, `;`, `#`, `\`, newline, quotes, unicode) | `_escape_ffmetadata` escapes the reserved set before writing the chapter file (§4b); `# VERIFY:` the exact set. Unicode titles pass through (ffmetadata is UTF-8). |
| **Path with quotes/unicode** in concat list | Single-quote + escape per concat-demuxer rules; absolute paths (§4a); `# VERIFY:` on Windows. |
| **`audio_cache_key` set but WAV deleted** | Caught by §2 precheck (`cache.has` check) → FAILED before any encode. |
| **Re-run after synthesize re-rendered a segment** (new key) | Precheck passes (segment is COMPLETED with the new key + file); assemble rebuilds the M4B from current keys. No stale-WAV problem — it reads live `audio_cache_key`s. |
| **Stop during build** | `build_assembly_request` returns `(None, 0)` → STOPPED, nothing written, status unset, resumable. |

---

## 10. Test plan (for the tester — fully offline/deterministic, no ffmpeg)

Everything is exercised through a **`FakeM4BAssembler`** and the **existing tiny silent WAV
fixtures**. Real ffmpeg/ffprobe/mutagen calls are `# VERIFY:` and are **never** run in CI.

### 10a. `FakeM4BAssembler` (new, in `tests/fakes/fake_assembler.py`, exported from `tests/fakes/__init__.py`)
Records exactly what it was handed **without invoking ffmpeg**:
- `__init__(*, available: bool = True, fail: bool = False)`.
- `is_available() -> bool` returns `available`.
- `assemble(request: AssemblyRequest) -> Path`: append `request` to `self.requests`; if `fail`, raise
  `AssemblyError("fake assembly failure")`; else **write a tiny placeholder file** at
  `request.out_path` (so "output exists" assertions pass and the atomic-move path is exercised) and
  return `request.out_path`.
Expose `self.requests: list[AssemblyRequest]` so tests assert on ordering / markers / cover / out_path.

### 10b. `assemble_ready_project` fixture (in `conftest.py`)
A saved project with `stage_status[PARSE/CORRECT/ATTRIBUTE/REVIEW/SYNTHESIZE]=COMPLETED`, whose every
renderable segment has an `audio_cache_key` **and a real tiny WAV written at
`AudioCache.path_for_key(key)`** (reuse `_write_silent_wav`, giving each segment a distinct known
duration so chapter-timing assertions are exact), `audio_status=COMPLETED`, plus **one whitespace-only
segment** (no key, PENDING) to prove it's skipped. Two chapters. Provide an
**unsynthesized variant** (`assemble_unrendered_project`) where one renderable segment has
`audio_cache_key=None` / `audio_status=PENDING` (or its WAV missing) for the precheck test. A
**cover variant** (`assemble_with_cover_project`) whose `book.cover_image_path` points at a tiny
generated PNG/JPEG (read-only input) for the cover test.

> Build these by hand (parse is a stub) mirroring `synthesize_ready_project`, but with WAVs **already
> present** in `audio/` at the segments' `audio_cache_key` paths (i.e. simulate a completed synthesize
> — either run `SynthesizeStage` with `FakeTTSProvider` in the fixture, or plant the WAVs + keys
> directly). Planting directly with `AudioCache.compute_key` keeps the fixture independent of the
> synthesize stage.

### 10c. `tests/pipeline/test_assemble_stage.py` (integration), asserting via the reloaded project + `FakeM4BAssembler`
1. **Precheck FAILS naming unrendered:** `assemble_unrendered_project` ⇒ `StageResult(ASSEMBLE,
   FAILED)`, message mentions unrendered segments, `stage_status[ASSEMBLE]` unset, `FakeM4BAssembler`
   never called (no `assemble`), no output file.
2. **Precheck PASSES when all rendered:** `assemble_ready_project` ⇒ COMPLETED;
   `stage_status[ASSEMBLE]==COMPLETED` (reload proves persistence); output file exists at
   `output/<sanitized title>.m4b`.
3. **Ordering:** the single `FakeM4BAssembler.requests[0].segment_audio_paths` equals the expected
   chapter→line→segment reading order of the rendered WAV paths, **whitespace segment excluded**, and
   each path equals `AudioCache.path_for_key(seg.audio_cache_key)`.
4. **Chapter markers:** `request.chapters` has one `ChapterMarker` per chapter with correct titles,
   `start_s` **monotonically non-decreasing**, and start/end computed from the fixture WAV durations +
   the gap constants (assert exact values given known fixture durations).
5. **Cover embedded iff set+exists:** `assemble_with_cover_project` ⇒
   `request.cover_image_path == Path(book.cover_image_path)`; `assemble_ready_project` (no cover) ⇒
   `request.cover_image_path is None`. (Assert the cover file was NOT copied into the workspace.)
6. **Output path:** `request.out_path == layout.output_dir / "<sanitized title>.m4b"`; a project with a
   hostile/unicode/empty title yields the sanitized/fallback name (dedicated cases).
7. **ffmpeg missing ⇒ FAILED:** `FakeM4BAssembler(available=False)` ⇒ FAILED, clear "install ffmpeg"
   message, status unset, `assemble` never called.
8. **Assembler failure ⇒ FAILED:** `FakeM4BAssembler(fail=True)` ⇒ FAILED (message from
   `AssemblyError`), status unset, no COMPLETED flag; no half-written M4B left at `out_path`.
9. **Stop/resume:** `RecordingProgressReporter(stop=True)` ⇒ STOPPED during build, `assemble` never
   called, nothing written, status unset; resume (fresh reporter) ⇒ COMPLETED.
10. **Zero renderable segments (whole book)** ⇒ FAILED ("no rendered audio"), `assemble` never called.
11. **Empty chapter** ⇒ a zero-length `ChapterMarker` present for it; ordering/timing of the other
    chapter unaffected.
12. **Persisted-then-reloaded:** `store.load()` after COMPLETED shows `stage_status[ASSEMBLE]`;
    segments' `audio_cache_key`/`audio_status`, book fields, and speakers are **untouched**;
    `CURRENT_SCHEMA_VERSION == 1`.
13. **`is_complete` / `next_stage`:** True after COMPLETED; in `Pipeline([..., Synthesize, Assemble])`
    `next_stage` returns `None` once assemble is COMPLETED; a direct re-run behavior per DECISION #6.
14. **No providers needed:** `StageContext(llm=None, tts=None)` runs assemble fine (it touches neither).

### 10d. `tests/audio/test_assemble_orchestration.py` (unit, pure, no stage)
- `is_renderable` / whitespace-skip parity with synthesize (same predicate).
- `wav_duration_s` returns the exact fixture WAV duration (from `wave` headers).
- `unrendered_segments` returns `[]` when all rendered; names the missing ones for each of the three
  failure conditions (no key / not-rendered status / missing file).
- `build_assembly_request`: ordering, markers (start/end from durations+gaps), cover resolution,
  out_path/sanitize, stop-returns-`(None,0)`.
- `sanitize_filename`: reserved chars, unicode preserved, empty ⇒ `"audiobook"`.
- `_escape_ffmetadata`: escapes `=`, `;`, `#`, `\`, newline (offline string test).

### 10e. `M4BAssembler` unit test (only the mockable parts) — `tests/audio/test_assembler.py`
- `is_available()` True/False via monkeypatching `shutil.which` (no real ffmpeg).
- **Mock the subprocess boundary** (`monkeypatch` `subprocess.run` to a fake returning rc=0, and stub
  the lazily-imported `mutagen.mp4.MP4`) and assert: the concat list file is written with the WAV
  paths in order; the ffmetadata chapter file contains one escaped `[CHAPTER]` block per marker with
  correct START/END; a non-zero ffmpeg rc ⇒ `AssemblyError`; the cover branch calls the (stubbed)
  MP4 cover API only when a cover is provided. The **real** ffmpeg/mutagen calls stay `# VERIFY:` and
  are exercised only through these mocks in CI.

### 10f. Lazy-import (`tests/test_lazy_imports.py`) — add cases
```python
("casttrophizer.audio.assembler", "mutagen"),      # mutagen imported lazily inside assemble()
("casttrophizer.audio.assembler", "PySide6"),
("casttrophizer.audio.assemble", "mutagen"),
("casttrophizer.audio.assemble", "PySide6"),
("casttrophizer.pipeline.stages.assemble", "mutagen"),
("casttrophizer.pipeline.stages.assemble", "PySide6"),
("casttrophizer.pipeline.stages.assemble", "torch"),
```
(mutagen IS installed, so these are meaningful — like the anthropic/openai cases.)

---

## 11. Ordered task list (coder)

Dependency order: 0 (decisions) → 1 (orchestration) → 2 (assembler) → 3 (stage uses 1+2) →
4 (fakes/fixtures/tests) → 5 (tooling).

0. **Get user sign-off** on the DECISIONS list below (cover source; real assembler now; fail-fast
   precondition; duration source; padding defaults; re-run idempotency; sanitize/marker minors).
1. **Orchestration** — `src/casttrophizer/audio/assemble.py`: `is_renderable(segment)` (shared with
   synthesize, or a re-export), `wav_duration_s(path)`, `unrendered_segments(project, cache)`,
   `sanitize_filename(title)`, `_escape_ffmetadata(text)`, gap constants, and
   `build_assembly_request(project, cache, layout, *, progress, should_stop) -> (AssemblyRequest |
   None, int)` — ordering (§3a), durations (§3b), markers+gaps (§3c/§3d), cover resolution (§5),
   out_path (§7), stop polling (§6a). Pure/offline; no ffmpeg. This is the seam the stage + most tests
   depend on. Fully testable on the fixture WAVs.
2. **`M4BAssembler.assemble`** — fill `src/casttrophizer/audio/assembler.py::assemble(request)`:
   write the build dir + concat list + ffmetadata chapter file, shell ffmpeg (concat demuxer → AAC/M4B,
   `# VERIFY:` all flags), embed cover + tags via **lazy** mutagen (`# VERIFY:` the MP4 API), atomic
   move into `output/`, clean up, wrap failures in `AssemblyError`. Keep `is_available` as-is. **All
   real command construction gets `# VERIFY:` comments** — the coder cannot run ffmpeg or web-verify.
3. **AssembleStage** — fill `pipeline/stages/assemble.py`: `__init__(assembler=None)`, status-driven
   `is_complete`, and `run` (§6): `is_available` guard, precheck, offline build, encode, FAILED/STOPPED
   paths, persist + COMPLETED. Qt-free; no `ctx.tts`/`ctx.llm`.
4. **Fakes + fixtures + tests** — `tests/fakes/fake_assembler.py` (`FakeM4BAssembler`) + export;
   `assemble_ready_project` / `assemble_unrendered_project` / `assemble_with_cover_project` fixtures
   (+ a tiny PNG/JPEG generator for the cover) in `conftest.py`;
   `tests/pipeline/test_assemble_stage.py`, `tests/audio/test_assemble_orchestration.py`,
   `tests/audio/test_assembler.py` (§10).
5. **Tooling** — `ruff check`, `black --check`, `mypy src`, `pytest` green; add the §10f lazy-import
   cases so `audio.assembler` / `audio.assemble` / `pipeline.stages.assemble` pull in no
   mutagen/torch/PySide6 at import.

---

## 12. Risks & open questions

- **ffmpeg flags/format unverifiable offline.** The concat-demuxer invocation, AAC/M4B codec + bitrate,
  ffmetadata chapter syntax (TIMEBASE/START/END), and escaping rules cannot be run or web-verified in
  this environment. Every real command gets a `# VERIFY:` comment; the user confirms at runtime on a
  machine with ffmpeg. This is the single biggest unknown — the *structure* (precheck, ordering,
  markers, request) is fully tested offline; the *exact ffmpeg strings* are not.
- **mutagen MP4 chapter/cover API** (`MP4`, `MP4Cover`, cover-format detection, tag atoms) — verify
  against installed `mutagen` at runtime; wrapped in `# VERIFY:` and mocked in CI.
- **EPUB cover extraction is unbuilt** (parse leaves `cover_image_path=None`). With policy (c) a normal
  book renders **coverless** until either the parse stage extracts the cover or a UI cover-picker sets
  the path. Both are **separate future work** — the biggest cross-stage gap, flagged.
- **AAC re-encode drifts sample-exact chapter boundaries** slightly vs. the `wave`-computed markers.
  Markers are navigation aids, not sample-exact — acceptable. If sample-exactness is ever required,
  probe the encoded file's chapter offsets post-hoc (out of scope).
- **No mid-encode stop** (§6a) — the ffmpeg subprocess runs to completion once launched. Killable
  encode needs process handling; deferred.
- **Per-speaker/per-chapter intermediate audio not cached** across assemble re-runs (§6b) — a re-run
  rebuilds the one M4B from the cached segment WAVs. Cheap enough; caching per-chapter concatenations
  is a possible future optimization (flagged, not built).
- **Scope-creep guard:** do NOT build EPUB cover extraction, a UI cover-picker, per-speaker/per-chapter
  audio caching, a killable-encode/cancel path, config-backed bitrate/padding, `ffprobe` support, a
  `Segment.duration_s` schema field, or loudness normalization here. This stage stops at: precheck
  passes → ordered segment WAVs + chapter markers + optional cover → one `output/<title>.m4b` →
  `stage_status[ASSEMBLE]=COMPLETED`.

---

## 13. Verification strategy (what the tester must prove)

1. **Precondition:** unrendered/missing-WAV segments ⇒ FAILED naming them, status unset, no ffmpeg,
   no output; all-rendered ⇒ passes. Whitespace segments skipped identically to synthesize.
2. **Ordering:** segments handed to the assembler in exact chapter→line→segment reading order with
   correctly resolved `AudioCache` WAV paths (whitespace excluded).
3. **Chapter markers:** one per chapter, correct titles, monotonically non-decreasing `start_s`,
   start/end computed from (fixture) durations + gap constants; empty chapter ⇒ zero-length marker.
4. **Cover:** embedded iff `cover_image_path` set + file exists; coverless otherwise (no failure);
   cover file not copied into the workspace (read-only).
5. **Output:** `output/<sanitized title>.m4b`; hostile/unicode/empty titles sanitized/fallback.
6. **Failure paths:** ffmpeg missing (`is_available` False) ⇒ FAILED "install ffmpeg"; assembler error
   ⇒ FAILED, no half-written M4B; no COMPLETED flag on either.
7. **Stop/resume:** STOPPED during build persists nothing/leaves status unset; resume completes.
8. **Persistence & purity:** reload proves `stage_status[ASSEMBLE]`; segment/book/speaker fields
   untouched; no schema bump (`CURRENT_SCHEMA_VERSION == 1`); artifacts only under the workspace;
   inputs read-only.
9. **Offline & lazy:** all tests use `FakeM4BAssembler` (or mocked subprocess/mutagen) — no real
   ffmpeg/ffprobe/mutagen; `audio.assembler` / `audio.assemble` / `pipeline.stages.assemble` import no
   mutagen/torch/PySide6.
10. `ruff` / `black --check` / `mypy src` / `pytest` green.

---

## Handoff

**Coder, once decisions land, build Task 1 first:** the offline orchestration in
`src/casttrophizer/audio/assemble.py` — specifically `unrendered_segments(project, cache)` (the §2
precheck, reusing `RENDERED_STATUSES` + the whitespace rule from `audio/synthesize.py` so
"renderable"/"rendered" mean the same in both stages), `wav_duration_s` (stdlib `wave`), and
`build_assembly_request(...)` (ordering §3a, durations §3b, chapter markers §3c, gap padding §3d,
cover resolution §5, sanitized out_path §7, stop polling §6a) returning a fully-computed
`AssemblyRequest`. It is the seam the stage and nearly every test depend on, and it is fully offline
against the existing tiny silent-WAV fixtures — no ffmpeg. Then fill `M4BAssembler.assemble` (Task 2,
every real ffmpeg/mutagen call marked `# VERIFY:`), wire `AssembleStage` (Task 3, `assembler=None`
injection), and finish with the `FakeM4BAssembler` + fixtures + tests (Task 4). Do NOT build EPUB
cover extraction, a UI cover-picker, or intermediate-audio caching here.

---

## DECISIONS FOR USER — CONFIRMED (build to these)

1. **Cover-art source: CONFIRMED — optional embed-if-set.** Assemble embeds `book.cover_image_path`
   iff it's set AND the file exists, else produces a coverless M4B; a bad-format/unreadable cover is
   **skipped with a warning (still produces the M4B)**. EPUB-cover extraction (parse) + a UI
   cover-picker are separate future work — so for now every book is coverless unless a path is set.
2. **Real `M4BAssembler` now: CONFIRMED — implement the real `assemble` in this PR** (ffmpeg
   concat-demuxer → AAC/M4B + ffmetadata chapters + lazy `mutagen` cover/tags). Mark **every** real
   ffmpeg/mutagen command/arg-construction with a `# VERIFY:` comment (exact flags, concat approach,
   ffmetadata `[CHAPTER]` format, AAC/M4B codec args, mutagen cover atom) for the user to confirm on a
   machine with ffmpeg. `is_available()` stays a cheap `shutil.which` (no subprocess). Do NOT run real
   ffmpeg in CI — all tests use `FakeM4BAssembler` / a mocked subprocess boundary.
3. **Unrendered-segment precondition: CONFIRMED — fail-fast.** Any renderable segment lacking a
   COMPLETED/APPROVED `audio_cache_key` with an on-disk WAV ⇒ FAILED ("run synthesize first / N
   segments not rendered"), status unset, nothing produced.
4. **Duration source: CONFIRMED — stdlib `wave`** (dependency-free, exact for our PCM WAVs, offline).
5. **Padding defaults: CONFIRMED — 0.0s inter-segment, 0.4s inter-line, 1.0s inter-chapter**, as fixed
   module constants for v1 (not surfaced in config/UI yet).
6. **Re-run idempotency: CONFIRMED — always rebuild on a direct `run`** (the `Pipeline` path stays
   idempotent via status-driven `is_complete`); no cross-run caching of intermediate per-chapter audio.
7. **(Minor) CONFIRMED —** filename: strip reserved chars, preserve unicode letters, fall back to
   `"audiobook"`; empty chapter emits a **zero-length marker** (1:1 with the book); segment order = the
   parsed **list order** (as synthesize uses), not a re-sort by `.order`.
