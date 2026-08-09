# Plan: Speaker voice categories + `castrun assign-voice --rest`

## Goal

Let a user voice a large cast by assigning distinct clips to the recurring speakers
individually, then run one command that assigns category-appropriate default clips
(man / woman / boy / girl / fallback) to every still-unvoiced speaker — so a 23-speaker
book clears the review gate without hand-assigning each minor character.

## Pipeline location

Touches two stages of `parse → correct → segment & attribute → review → synthesize → assemble`:

- **attribute** — the crux. Speakers are discovered here (`resolve_speaker`). We add a
  one-shot per-speaker classification pass at the *end* of the attribute stage that stamps
  each `Speaker.category`. State/resumability preserved: classification runs on the pass
  that finishes attribution, immediately before `stage_status[ATTRIBUTE]=COMPLETED`, and is
  soft (failure leaves categories `unknown`, never fails the stage).
- **review** — `assign-voice --rest` (a review-time bulk action, like the existing
  `assign-voice`) clears gate criterion 3 (`unresolved_voices`) for the remaining speakers.

No change to `synthesize`/`assemble`: a `--rest`-assigned speaker is an ordinary voiced
speaker; each `VoiceClip` points at a real reference path, so `AudioCache.key_for` and TTS
behave exactly as today (multiple speakers sharing one clip path is fine and expected).

---

## Decision 1 — Category taxonomy

**Recommend (v1):** a strict `StrEnum` `VoiceCategory` with exactly
`man / woman / boy / girl / unknown`, in `domain/enums.py`.

- `unknown` is the fallback for the narrator, unnamed/ambiguous, non-human, or
  classification-failed speakers.
- **Strict enum, not an open string.** Rationale: (a) matches the existing `SpeakerRole`
  / `StrEnum` convention — trivial serialization, load-time validation catches typos; (b)
  the enum *is* the `--rest` flag surface — a finite set means a finite, documented set of
  CLI flags (`--man/--woman/--boy/--girl/--default`); an open string would make that
  surface unbounded. **FLAG.**
- **Future (fantasy/scifi "orc man vs elf man"):** the `Speaker.category` field is the
  extension seam. Extending the taxonomy later is a schema bump that adds enum members
  (e.g. `OTHER`, or richer values) plus new `--rest` flags — isolated to `domain/enums.py`
  + the CLI, no pipeline change. v1 deliberately stays at the simple 5.

```python
# domain/enums.py
class VoiceCategory(StrEnum):
    MAN = "man"
    WOMAN = "woman"
    BOY = "boy"
    GIRL = "girl"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: object) -> "VoiceCategory":
        """Map an arbitrary/LLM string to a VoiceCategory, defaulting to UNKNOWN."""
        try:
            return cls(str(value).strip().casefold())
        except ValueError:
            return cls.UNKNOWN
```

Add `"VoiceCategory"` to `enums.__all__`.

---

## Decision 2 — Where categorization happens (the crux)

**Recommend: a separate, post-attribution, one-shot per-speaker classification pass**
(option b), not folding a `category` into the per-segment attribution response (option a).

Why:

- Category is a **per-speaker** property; attribution returns **per-segment** speaker
  proposals. Folding category into per-segment responses means the same speaker is
  re-classified on every segment, inviting contradictions and wasting tokens on narration
  that is never a character.
- A single pass classifies *all* discovered `CHARACTER` speakers at once (each with a
  small dialogue sample) → **one extra LLM call per book**, cheap and isolated.
- Clean failure surface: classification is non-critical, so any provider error → leave
  `unknown`, don't touch the attribution result or the stage status.

**FLAG.** (Cost: one extra call. Accuracy: name + a few sampled lines is enough for
gender/age bucketing; the user still overrides via individual `assign-voice`.)

### LLM contract (new provider seam)

Add to `providers/base.py` (import-cheap, Qt-/SDK-free — DTOs + one method with a **safe
default**, so the ABC change is non-breaking and LMStudio keeps working):

```python
@dataclass
class SpeakerProfile:
    speaker_id: str
    name: str
    samples: list[str] = field(default_factory=list)   # a few dialogue lines

@dataclass
class SpeakerClassification:
    speaker_id: str
    category: str          # free string; caller maps via VoiceCategory.coerce
    confidence: float = 0.0
    rationale: str = ""

class LLMProvider(ABC):
    ...
    def classify_speakers(
        self, *, speakers: list[SpeakerProfile]
    ) -> list[SpeakerClassification]:
        """Bucket each speaker into a voice category (man/woman/boy/girl/unknown).

        Default implementation classifies everyone 'unknown' — a safe no-op so a provider
        without a real classifier (e.g. LMStudio v1) degrades to '--default' rather than
        failing. Concrete providers (Claude) override with a real one-shot call.
        """
        return [
            SpeakerClassification(speaker_id=s.speaker_id, category="unknown")
            for s in speakers
        ]
```

Add `SpeakerProfile`, `SpeakerClassification` to `base.__all__`.

**Claude override** (`providers/llm/claude.py`), mirroring `attribute_speakers`:

- System prompt: "Bucket each listed speaker into one of: man, woman, boy, girl, unknown,
  using the name and sample dialogue. Use `unknown` when gender/age is unclear or the
  speaker is non-human." Sends a numbered list keyed by `speaker_id`.
- `output_config` json_schema: array of `{speaker_id, category (enum
  man|woman|boy|girl|unknown), confidence, rationale?}`, one entry per requested id.
- Parse identically to `_parse_attributions`: enforce one-per-requested-id (missing →
  `unknown`, 0.0), drop unrequested ids. Non-JSON/wrong-shape → `LLMProviderError(malformed=True)`;
  reachability → plain `LLMProviderError`. The orchestration below catches **both** (this
  pass is non-critical) and falls back to `unknown`.

**Mapping to the domain:** the orchestration maps each `SpeakerClassification.category`
string → `VoiceCategory.coerce(...)` and writes `speaker.category`. The narrator is never
sent (always `VoiceCategory.UNKNOWN`).

### Orchestration (new module, mirrors `attribution/attribute.py`)

`attribution/classify.py`:

```python
CLASSIFY_SAMPLE_LINES = 3   # max dialogue samples per speaker sent to the LLM

def classify_speakers(project: Project, llm: LLMProvider) -> None:
    """Classify project CHARACTER speakers into VoiceCategory in place (one LLM call).

    Builds a SpeakerProfile per CHARACTER speaker whose category is still UNKNOWN, sampling
    up to CLASSIFY_SAMPLE_LINES of that speaker's own segment texts. Calls
    llm.classify_speakers once; maps each result via VoiceCategory.coerce onto the speaker.
    Soft: ANY LLMProviderError (malformed or unreachable) leaves categories UNKNOWN and
    returns — classification never fails the attribute stage. Narrator is left UNKNOWN.
    """
```

Export `classify_speakers` and `CLASSIFY_SAMPLE_LINES` from `attribution/__init__.py`.

**Determinism / offline:** the `FakeLLMProvider` overrides `classify_speakers` from a
`classify_script` (keyed by speaker name) so tests script exact categories with no network.

---

## Decision 3 — Schema change

Bump `CURRENT_SCHEMA_VERSION` **1 → 2** in `domain/serialization.py`.

- **Model:** add `category: VoiceCategory = VoiceCategory.UNKNOWN` to `Speaker`
  (`domain/models.py`). Default keeps every in-code `Speaker(...)` construction (tests,
  policy, review actions) compiling unchanged. Import `VoiceCategory` there.
- **`_speaker_to_dict`:** add `"category": str(sp.category)`.
- **`_speaker_from_dict`:** `category=VoiceCategory(d["category"])` (present post-migration).
- **Migration** — replace the "no historical migrations" body of `_migrate` with a real
  step chain:

```python
def _migrate(data, version):
    if version > CURRENT_SCHEMA_VERSION:
        raise MigrationError(...newer than supported...)
    if version < 1:
        raise MigrationError(...no path...)
    if version == 1:
        data = _migrate_v1_to_v2(data)   # add category=unknown to every speaker
        version = 2
    # future: if version == 2: data = _migrate_v2_to_v3(data); version = 3
    if version != CURRENT_SCHEMA_VERSION:
        raise MigrationError(...no path...)
    return data

def _migrate_v1_to_v2(data):
    for sp in data.get("speakers", []):
        sp.setdefault("category", "unknown")
    return data
```

- **Round-trip:** `project_from_dict(project_to_dict(p)) == p` holds because `category`
  is stamped and re-read; dataclass `__eq__` now includes it.
- **No other model changes.** `VoiceClip`, `Segment`, `Line`, etc. are untouched — a
  category is a property of the *speaker*, and the default clip is just an ordinary
  `VoiceClip` assigned to the speaker.

---

## Decision 4 — CLI `assign-voice --rest`

**Surface (recommend: flags + optional env/config defaults; flags win).** **FLAG.**

Extend the existing `assign-voice` subparser (keep the positional single-speaker path
intact; add a `--rest` mode):

```
castrun assign-voice SPEAKER CLIP           # unchanged single-assign path
castrun assign-voice --rest \
    [--man PATH] [--woman PATH] [--boy PATH] [--girl PATH] [--default PATH]
```

- Make the positionals `nargs="?"` (default `None`). In `cmd_assign_voice`, branch:
  `--rest` present → bulk path (error if a positional was also given); else require both
  positionals (today's behavior, explicit error if missing).
- **Env/config defaults (v1, recommended):** add `AppConfig` fields
  `voice_defaults: dict[str, str]` populated in `from_env` from
  `CASTTROPHIZER_VOICE_MAN / _WOMAN / _BOY / _GIRL / _DEFAULT`. A missing flag falls back
  to the config/env value; an explicit flag overrides it. This directly answers "so paths
  aren't retyped every run." (If scope must shrink, drop this to flags-only — the bulk
  logic is unchanged; only where the map is sourced differs.)

**Behavior of the `--rest` path (`cmd_assign_voice`):**

1. Load project via `_require_store`. Compute the **target set** = referenced, unvoiced
   speaker *objects* via a new `audio.synthesize.unresolved_speakers(project)` helper (see
   Decision 5) — this matches the gate exactly and excludes the `<unattributed>` sentinel
   (which `--rest` cannot fix; those stay review blockers).
2. Build the category→path map from flags, falling back per-key to env/config.
   `VoiceCategory.UNKNOWN` maps to `--default`. Any category with no clip and no
   `--default` is **uncovered**.
3. **Dry-run validation before mutating:** for each target speaker resolve
   `path = map.get(speaker.category) or map.get(default)`. Collect
   `(speaker.name, speaker.category)` for every target whose category is uncovered. If any
   are uncovered → raise `CliError` (exit 1) naming the uncovered categories and the
   speakers under them, e.g.
   `"no clip for categories: woman (speakers: Sally, Ida); pass --woman or --default"`.
   Nothing is assigned (do NOT silently leave them unvoiced — the gate would re-block).
4. Validate each *used* path once with `actions.register_voice_clip` (raises `ValueError`
   on a bad path → `CliError`). Register **one `VoiceClip` per distinct path** actually
   used and reuse it across all speakers of that category (fewer records; sharing a clip is
   fine). Label e.g. `"man (default)"`.
5. Assign via pure `actions.assign_voice(speaker, clip)` in memory, then **persist once**
   with `store.save(project)` — mirrors `_auto_accept`'s single-atomic-write pattern (a
   20-speaker book shouldn't fsync per speaker). Assigning only *clears* blockers, so no
   review-flag invalidation is needed (matches `ReviewService.assign_voice`, which does not
   invalidate). **FLAG:** this deviates slightly from "persist via the review service" for
   batch efficiency; alternative is adding `ReviewService.assign_voices(...)` (batch, single
   save) and calling that — either is acceptable; recommend the small `ReviewService` batch
   method so persistence stays owned by the service.
6. Print a summary: `assigned defaults to N speaker(s): man×3, woman×2, unknown×1`.

**Edge outcomes** (Decision 7): no targets → no-op with a clear message; no clips provided
at all → uncovered error.

---

## Decision 5 — Interaction with the existing flow

- `--rest` is run **after** individual `assign-voice` calls, so its target set (referenced
  + unvoiced) only touches the still-unvoiced remainder. It **clears gate criterion 3** for
  those speakers (`unresolved_voices` shrinks to `[]` once every referenced speaker,
  narrator included, is voiced), so `castrun run --auto-accept` then proceeds through
  review → synthesize → assemble. **No change to `--auto-accept` semantics** (it still only
  touches criteria 1 & 2; voices remain a manual/`--rest` action).
- New helper `audio.synthesize.unresolved_speakers(project) -> list[Speaker]`: the
  Speaker-object analogue of `unresolved_voices`, sharing `_referenced_speaker_ids` +
  `_resolved_clip_path` so review, synthesize, and `--rest` assert one predicate (no drift).
  Optionally re-express `unresolved_voices` in terms of it (`[sp.name for sp in
  unresolved_speakers(project)]` + the `<unattributed>` sentinel) to guarantee no drift.
- `castrun speakers` prints the category so the user can see who got classified as what:
  `[i] Name (character) [woman] - NO VOICE`. (Purely display; add `speaker.category.value`.)
- Optional nicety (not required): in `_report_run`'s NEEDS_REVIEW branch, when voices are
  the only remaining blocker, hint `assign-voice --rest --default <clip>`.

---

## Decision 6 — Re-run / backward compat

**Recommend the simplest path: `unknown` → `--default`, no new command.** **FLAG.**

- Pre-feature projects migrate (v1→v2) with every speaker `category=unknown`. Under
  `--rest`, `unknown` maps to `--default`, so `castrun assign-voice --rest --default X`
  voices the whole cast immediately — no re-attribution required to *ship*.
- To get **real** categories on an old project, re-run attribution on a fresh project
  (attribute is skip-on-existing-segments + status-gated, so `castrun run` will **not**
  re-attribute a COMPLETED project). This is acceptable: categories are a convenience for
  distinct minor-cast voices, and `--default` always works.
- **Stretch (optional, not v1):** a `castrun classify` command that runs `classify_speakers`
  standalone over an already-attributed project, letting an old project pick up categories
  without re-attributing. Design it if the user wants it; the orchestration already exists
  as a reusable function.

---

## Decision 7 — Edge cases (all handled above; explicit list)

| Case | Behavior |
|---|---|
| LLM can't determine a category | → `unknown` → `--default` |
| Narrator unvoiced | user assigns explicitly, or `unknown` → `--default` under `--rest` |
| Book with only `unknown` speakers | all → `--default` (error if no `--default`) |
| `--rest` when everyone is already voiced | no-op; "all referenced speakers already voiced" |
| `--rest` with no clips provided at all | uncovered error naming the categories/speakers |
| A category covered but `--default` absent, and one speaker is `unknown` | `unknown` is uncovered → error (needs `--default`) |
| `<unattributed>` segments remain | `--rest` cannot fix (no speaker); still a review blocker, reported normally |
| Same clip path for several speakers | fine — one shared `VoiceClip`, same reference voice |

---

## Ordered tasks (for the coder)

1. **`domain/enums.py`** — add `VoiceCategory` StrEnum (+ `coerce`) and export it.
2. **`domain/models.py`** — add `category: VoiceCategory = VoiceCategory.UNKNOWN` to
   `Speaker`; import `VoiceCategory`.
3. **`domain/serialization.py`** — bump `CURRENT_SCHEMA_VERSION=2`; serialize/deserialize
   `category`; add `_migrate_v1_to_v2` and rewrite `_migrate` as a version-step chain.
4. **`providers/base.py`** — add `SpeakerProfile`, `SpeakerClassification` DTOs; add
   `LLMProvider.classify_speakers` with the safe default-`unknown` impl; extend `__all__`.
5. **`providers/llm/claude.py`** — override `classify_speakers` (system prompt +
   json_schema + one-per-id parse, mirroring `attribute_speakers`; malformed vs reachability
   error handling identical). LMStudio inherits the default (no change).
6. **`attribution/classify.py`** (new) — `classify_speakers(project, llm)` orchestration +
   `CLASSIFY_SAMPLE_LINES`; soft-fail on any `LLMProviderError`. Export from
   `attribution/__init__.py`.
7. **`pipeline/stages/attribute.py`** — after the chapter loop and before stamping
   `COMPLETED`, call `classify_speakers(project, ctx.llm)` (inside the existing
   try/except is unnecessary — the orchestration is self-soft; just call it, then
   `ctx.store.save(project)`), so a finished attribution run also classifies. Update the
   stage docstring.
8. **`audio/synthesize.py`** — add `unresolved_speakers(project) -> list[Speaker]`; keep
   `unresolved_voices` behavior (optionally re-express it in terms of the new helper).
   Export it.
9. **`config.py`** (if env/config defaults in scope) — add `voice_defaults` field +
   `CASTTROPHIZER_VOICE_*` parsing in `from_env`.
10. **`review/service.py`** (recommended) — add `assign_voices(pairs)` batch method: apply
    `actions.assign_voice` for each, single `_save()`. (Or skip and use the
    `store.save`-once pattern directly in the CLI.)
11. **`cli.py`** — extend the `assign-voice` subparser (`--rest`, `--man/--woman/--boy/
    --girl/--default`, positionals `nargs="?"`); split `cmd_assign_voice` into the existing
    single path + the new bulk path (target = `unresolved_speakers`, dry-run coverage check,
    register-once-per-path, batch assign, single save, summary print). Add
    `[{category}]` to `cmd_speakers` output.
12. **`tests/fakes/fake_llm.py`** — override `classify_speakers` driven by
    `classify_script: dict[str, tuple[str, float]]` (keyed by speaker name; default
    `unknown`); record `classify_calls`. Add optional `raise_classify_malformed`/
    `raise_classify_unreachable` toggles for the soft-fail test.

---

## Interfaces & data shapes (summary)

- `VoiceCategory(StrEnum)`: `man|woman|boy|girl|unknown` + `coerce(value) -> VoiceCategory`.
- `Speaker.category: VoiceCategory = VoiceCategory.UNKNOWN` (persisted; schema v2).
- `LLMProvider.classify_speakers(*, speakers: list[SpeakerProfile]) -> list[SpeakerClassification]`
  (default → all `unknown`; Claude overrides). Cross-provider boundary DTOs use plain
  strings for `category` (provider stays decoupled from the domain enum; caller coerces).
- `attribution.classify_speakers(project: Project, llm: LLMProvider) -> None` (in-place,
  soft, one call).
- `audio.synthesize.unresolved_speakers(project: Project) -> list[Speaker]`.
- CLI: `assign-voice --rest [--man/--woman/--boy/--girl/--default PATH]`.

## Threading note

No Qt in this change. Classification is one LLM call inside the **attribute stage**, which
already runs off the Qt main thread (worker/QThread per CLAUDE.md). `assign-voice --rest`
is a CLI/review-time action (cheap in-memory mutations + one atomic save) — the same class
of synchronous work as the existing `assign-voice`, which the design doc already states may
run on the Qt main thread. Nothing new needs a worker thread.

## Risks & open questions

- **ABC method addition.** `classify_speakers` is added as a *concrete* default (not
  `@abstractmethod`) so existing providers (LMStudio, any test doubles) don't break. If the
  team prefers it abstract, LMStudio needs an explicit impl too. **Recommend concrete
  default.**
- **LMStudio quality.** LMStudio inherits `unknown` for everyone (no real classifier in
  v1) → its users lean on `--default`. Acceptable per the "backup is a fallback" stance;
  flagged so it's a conscious choice.
- **Classification accuracy.** Name + a few samples is a heuristic; the user override
  (individual `assign-voice`) remains the source of truth. Low-confidence categories are
  *not* surfaced as review blockers in v1 (a wrong category only affects which *default*
  clip a minor character gets, and only if the user runs `--rest`). Open question: should
  low-confidence categories be surfaced in the review UI later? (Out of scope for v1.)
- **`--rest` target scope.** Restricting to *referenced* unvoiced speakers (via
  `unresolved_speakers`) avoids erroring on phantom/unreferenced speakers and matches the
  gate. Confirm that's the intended scope (vs. "every speaker in the list").
- **Env/config defaults scope.** Recommended in v1 (solves retyping) but easily deferred to
  flags-only. **Needs the user's call** (Decision 4).

## Verification strategy (for the tester — all offline, `FakeLLMProvider`/`FakeTTSProvider`/`FakeM4BAssembler`, no real API)

1. **Classification in attribute stage.** Run `SegmentAttributeStage` on
   `attribute_ready_project` with a `FakeLLMProvider(classify_script={"Alice":("woman",0.9),
   "Bob":("man",0.9)})`; assert discovered speakers get the scripted `category`, narrator
   stays `unknown`, and exactly **one** `classify_speakers` call is recorded.
2. **Soft-fail.** With `raise_classify_unreachable`/`raise_classify_malformed`, assert the
   stage still returns COMPLETED and all categories are `unknown` (classification never
   fails the stage).
3. **Serialization round-trip + migration.** `project_from_dict(project_to_dict(p)) == p`
   with categories set; and a hand-built **v1** dict (no `category` keys, `schema_version=1`)
   loads via `_migrate` with every speaker defaulted to `unknown` and re-serializes at v2.
4. **`assign-voice --rest` happy path.** On a project with mixed categories and some
   speakers pre-voiced, run `--rest --man M --woman W --default D`; assert each unvoiced
   speaker got the clip for its category (unknown → `D`), pre-voiced speakers untouched, and
   `unresolved_voices(project) == []`.
5. **Fallback + coverage error.** `--rest` with a `woman` target but only `--man`/no
   `--default` → non-zero exit, message names the uncovered category + speakers, and
   **nothing** was assigned (project unchanged on disk).
6. **Gate + full run.** After `--rest` clears criterion 3, `castrun run --auto-accept`
   (with `CliDeps` fakes) completes end-to-end to an M4B (via `FakeM4BAssembler`).
7. **No-op / empty cases.** `--rest` when every referenced speaker is already voiced →
   exit 0, "already voiced" message, project unchanged; `--rest` with no clip flags at all →
   coverage error.
8. **`speakers` shows categories.** `cmd_speakers` output includes `[woman]`/`[man]`/
   `[unknown]` per speaker.
9. **Shared clip.** Two same-category speakers under one path → both voiced, and (if the
   register-once optimization lands) they share one `VoiceClip` id / else two records both
   pointing at the same path — either way both resolve a voice.

---

## DECISIONS FOR USER — CONFIRMED (build to these)

1. **Taxonomy & type: CONFIRMED — `man / woman / boy / girl / unknown` as a strict `StrEnum`**
   (`VoiceCategory` in `domain/enums.py`). Fantasy/scifi expansion is a future schema bump on
   the same field — keep the field/design extensible but ship this set.
2. **Categorization approach: CONFIRMED — a separate post-attribution one-shot classification
   pass** (one extra LLM call per book), run at the END of `SegmentAttributeStage` right before
   it stamps COMPLETED. Soft: any provider error leaves categories `unknown` and never fails the
   stage. The narrator is `unknown`.
3. **`--rest` clip surface: CONFIRMED — flags + reusable env/config defaults**, flags win:
   `--man/--woman/--boy/--girl/--default` override `CASTTROPHIZER_VOICE_MAN/_WOMAN/_BOY/_GIRL/
   _DEFAULT` (parsed in `AppConfig.from_env`). So a user sets category clips once and later just
   runs `castrun assign-voice --rest`. A category with no flag AND no configured default falls
   back to `--default`/`CASTTROPHIZER_VOICE_DEFAULT`; if an unvoiced speaker's category is still
   uncovered, FAIL with a clear message naming the uncovered categories/speakers (never leave a
   referenced speaker unvoiced — the gate would still block).
4. **Pre-feature projects: CONFIRMED — `unknown` → `--default`** (no new command); re-run
   attribution on a fresh project to get real categories. The v1→v2 migration defaults existing
   speakers to `unknown`.
5. **`--rest` scope: CONFIRMED — targets only *referenced, currently-unvoiced* speakers** (the
   ones `unresolved_voices`/the gate flags), so it runs after the user has assigned the main cast
   individually and clears gate criterion 3 for the remainder.

---

**Handoff — build first:** Start with the domain layer in dependency order — task 1
(`VoiceCategory` enum), task 2 (`Speaker.category` field), task 3 (schema bump + v1→v2
migration) — and get the serialization round-trip + migration tests green before wiring the
provider/classification pass. That establishes the persisted shape everything else builds on.
