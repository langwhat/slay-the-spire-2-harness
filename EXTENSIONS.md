# Extension documents

Document providers integrate supplementary knowledge and guidance through the core’s existing typed interfaces.

## Responsibility

The core owns the game client, submitted actions, settlement, Run/Combat orchestration, notes, role contexts, model calls, and recording. A document provider supplies supplementary text and its associations. It does not submit game actions, edit model history, trigger context rebuilding, or construct SDK messages or cache parameters.

Three consumers use this one document interface: curated object knowledge, act/Boss guidance, and static monster move/transition rules. A shared Boss document can serve Run and Combat under the same identity. Static move rules do not infer the current native state and require no observation subscription or tracker. Live native observations remain the source for current card values, powers, intent, and available actions.

No third-party agent framework or extension event runtime is required. The current interface has no observation, combat-start, or combat-end callbacks. Providers may cache their own source data, but a cache is not evidence that a role context has received its contents.

## Provider and value types

The Python types live in [`sts2_harness.knowledge`](src/sts2_harness/knowledge.py) and [`sts2_harness.material_text`](src/sts2_harness/material_text.py). `Documents` combines these asynchronous capabilities:

| Method | Input | Result | Meaning |
| --- | --- | --- | --- |
| `list(role)` | `DecisionOwner` | `tuple[SkillSummary, ...]` | Discover navigable documents appropriate to the role. |
| `read(request)` | `SkillRead(id: str, resource: str \| None = None)` | `DocumentView` | Read the complete document or one advertised resource. |
| `lookup(subjects)` | `tuple[SubjectId, ...]` | `tuple[Document, ...]` | Select supplementary documents associated with native subjects. |

`SubjectId` contains a nonempty `category` and `definition_id`. Native object subjects use native category/definition IDs; act guidance also uses the `act` category. Document IDs identify provider documents independently of those subjects. A document may have several subjects, and several documents may apply to one subject.

`DocumentView` has `id`, `name: MaterialText`, `text: MaterialText`, `resources: tuple[Resource, ...]`, and an optional `resource` key. `Resource` has a key and a `MaterialText` description. `Document` adds `subjects` and exposes `view()` for delivery without lookup provenance. Resource keys are scoped to their parent document. For a root `DocumentView`, omit `resource` (`MISSING`); do not pass `None`. A resource view supplies its nonempty key. `SkillRead` uses `None` to request the root document. The internal delivery ledger identifies roots and resources separately as `(document.id, resource-or-None)`.

`SkillSummary` has `id`, `name: MaterialText`, and `description: MaterialText`. Discovery need not enumerate every object supplement. A document absent from a role's discovery list can still be selected by `lookup` or explicitly read by its configured identity. `lookup` has no role parameter; the caller supplies the subjects for its current purpose. Discovery filtering is not an authorization boundary.

`MaterialText` is an immutable `id`, `version`, and `body` snapshot. `MaterialText.create(id, body)` computes `version` as SHA-256 of the exact UTF-8 body. Keep IDs stable for the same logical material and create a new snapshot when its body changes. Names, body text, resource descriptions, and skill descriptions each retain their own material identity/version. Runtime model serialization emits the body; recording retains identity, version, and body separately.

`FileDocuments` is the existing explicit-file implementation. It takes `DocumentSource` and `ResourceSource` values, rejects duplicate document IDs or resource keys, reads configured UTF-8 files, and matches subjects without scanning a directory. Other providers implement the same capabilities using their own source formats and selection policies. The core prescribes no private corpus layout.

## Entry composition and lifecycle

Pass `EntryMaterials(documents: Documents, settings: SessionMaterials)` to [`sts2_harness.cli.main`](src/sts2_harness/cli.py) as the keyword `materials`: `main(argv=None, *, materials=None)`. Import `EntryMaterials` from `sts2_harness.entry_materials` and `SessionMaterials` from `sts2_harness.journal`. Builtin `start` and developer `headless` support it; external-driver `start` rejects supplied entry materials with `ConfigurationError`. `check-config` and `journal` do not read the provider. `SessionMaterials` records `entry_documents_enabled`, `act_guidance: dict[int, tuple[str, ...]]`, and `battle_guidance: tuple[str, ...]` in the session manifest. The same provider supplies entry guidance, skill reads, and native-lookup supplements through the recorded IO ports. Entry delivery can be disabled without changing the three provider methods.

At a new Run act context, the core reads configured act document IDs and looks up the public act identity plus all Boss encounter IDs known on the act map. It obtains a missing map through the existing inspect port and verifies that the run and act still match. Configured and subject-selected documents are combined by document ID.

At a fresh Combat context, the core reads configured battle document IDs and looks up the public encounter, current cards in all piles, relics, potions, enemies including inactive enemies, player/enemy/ally powers, and available orb IDs. Selection candidates are included when Combat owns the current menu. Selection uses public stable IDs and does not derive hidden game state. New or changed public battle objects can select further material before a later model request. A missing referenced current object is a content-loading failure, not silently omitted guidance.

The first selected documents are supplied as system `GuidanceContent` when that role's context is constructed. Later preparation reads select current documents again. `Context.append_materials` appends a user `GuidanceContent` containing only previously undelivered or changed complete `DocumentView` values. It compares the complete view, not just the main text hash. It never rewrites an earlier system or user snapshot. An empty selection does not retract earlier history.

Delivery belongs to the retained context. Run and Combat track it independently even when their providers or documents are shared. A new Combat gets a fresh context; its ordinary nonterminal review retains that battle context. Run rebuilds at act entry. Rebuilding permits the same material to be delivered again. Pause/resume or repeated preparation alone does not clear retained delivery. A failed replacement Run initialization leaves the previous context intact until a new one can be built successfully.

This lifecycle has no extension-specific reset hook.

## Explicit reads and native knowledge

The role's existing skill tool supports listing and reading. The core filters `list(role)` summaries by optional `text`, a case-insensitive substring of the public id, name, or description, with surrounding whitespace ignored. It sorts matching summaries by id and returns at most 20 per call. Tools accept `offset` (default 0) and `limit` (1–20, default 20); `total` counts all matches and `next_offset` is null at the end. Continue with the same text filter. Filtering does not read document bodies or change the provider interface. A resource read must name a resource advertised by its parent document. Explicit reads return the requested complete view even when the same content was delivered earlier; they are not replaced by a short “already read” result. `Context` records that actual tool result for subsequent automatic-delivery comparisons.

The knowledge tool first queries the native catalog. For a native lookup, the core passes the found native subjects to `Documents.lookup`, attaches matching document views to each native entry, and keeps native entry order and `missing_ids`. Each returned document must include the matched native subject in `subjects`; the core uses that association when attaching supplements to an entry. Supplements cannot introduce native matches or turn an absent private supplement into a missing native ID. Complete native definitions and current-state inspection remain separate capabilities. Shared supplementary document identities are deduplicated within each native entry. Actual explicit lookup results also update the context's delivery ledger.

This is a read-only document seam. A provider cannot acquire execution ownership by returning action-shaped text or associations. The core still validates and executes the role's action tools through its normal execution boundary.

## Empty results and failures

An unconfigured provider, an empty successful discovery or lookup, and no applicable entry guidance are normal outcomes. Return empty tuples for those cases. Raise `UnknownDocument` for an unknown document ID or unadvertised resource. Raise `MaterialUnavailable` (including `SupplementUnavailable`) for an enabled source that cannot be read or decoded. Do not substitute empty text or silently disable a failed source. Boundary errors should identify the logical document/resource without exposing private paths or content.

An explicit unknown read becomes an invalid-arguments tool result. An enabled read/lookup failure becomes `content_unavailable` and participates in the existing shared tool recovery budget. Entry initialization failure pauses before the first gameplay decision; user Start retries initialization after the source is repaired. Later automatic material failure uses the role's existing recovery budget and preserves its context. Providers do not add their own retry loop, fallback source, or pause policy. Lifetime and role changes detected while obtaining public selection inputs remain core orchestration events.

## Recording, reconstruction, and material variants

The current manifest schema is 10 and includes `SessionMaterials`. `RecordedDocuments` records each role listing, document/resource read, and subject lookup with its typed result or explicit unknown/unavailable failure. `DocumentSnapshot` retains names, bodies, resource descriptions, and lookup subjects when applicable; `MaterialSnapshot` retains the exact material ID/version/body. The journal stores these snapshots and assembled model inputs so that reconstruction does not reread today's files as yesterday's evidence.

Historical reconstruction uses `ReplayDocuments` and the recorded material timeline. It validates the requested operations, rebuilds the software prefix, and restores role delivery from actual messages. Replaying recorded output is sufficient for the current static readers.

The existing eval `MaterialVariant` declares `MaterialReplacement(material_id, expected_versions, text)` and either `HistoricalStart` or `SceneStart(initial_notes)`. Replacements keep the material ID and use a new exact-body hash. Preflight checks complete recorded source-version sets and rejects unknown, inconsistent, or undeclared versions. `VariantDocuments` applies declared replacements to list/read/lookup outputs while preserving document IDs, resource keys, and subjects. Historical variants retain the recorded software prefix with the declared material substitutions; scene variants build fresh role context at an admitted scene instead of claiming a historical prefix was delivered.

Use the existing eval admission, reconstruction, and suite paths for comparisons. A material variant preserves the recorded `SessionMaterials` settings and pre-target game operations. Disabling automatic entry delivery does not disable recorded explicit skill or knowledge reads; their recorded materials may still be replaced. The live continuation may make different decisions through the ordinary action tools. A variant does not invent unrecorded material dependencies. Invalid experiment material declarations are rejected at the eval boundary rather than repaired through gameplay tool recovery. A comparison must report its precise source settings, IDs, versions, start mode, and actual execution evidence.
