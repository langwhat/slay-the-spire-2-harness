# Extension documents

Extensions add knowledge and guidance to the core by supplying documents. This page is for anyone writing a document provider.

## What a provider does

The core owns the game client, actions, settlement, orchestration, notes, role contexts, model calls, and recording. A provider only supplies text and says which game subjects it relates to. It does not submit actions, edit model history, trigger context rebuilds, or build SDK messages or cache parameters.

One interface serves every kind of supplementary material, for example curated card knowledge, act and Boss guidance, and static monster move rules. A shared Boss document can serve both Run and Combat. Move rules describe how a monster behaves; current values, powers, intents, and available actions always come from live observations.

There are no observation or combat-start/end callbacks. A provider may cache its own source data, but its cache says nothing about what a role has actually received.

## Interface

The types live in [`sts2_harness.knowledge`](src/sts2_harness/knowledge.py) and [`sts2_harness.material_text`](src/sts2_harness/material_text.py). A `Documents` provider has three async methods:

| Method | Input | Result | Purpose |
| --- | --- | --- | --- |
| `list(role)` | `DecisionOwner` | `tuple[SkillSummary, ...]` | Documents to show this role in skill listings. |
| `read(request)` | `SkillRead(id, resource=None)` | `DocumentView` | One whole document, or one of its advertised resources. |
| `lookup(subjects)` | `tuple[SubjectId, ...]` | `tuple[Document, ...]` | Documents associated with native game subjects. |

The value types:

- **`SubjectId`** has a non-empty `category` and `definition_id`. Native objects use native categories and definition IDs; act guidance uses the `act` category. A document can have several subjects, and a subject can match several documents. Document IDs are separate from subjects.
- **`DocumentView`** has `id`, `name`, `text`, `resources`, and an optional `resource` key. For a root document, leave `resource` unset (`MISSING`), not `None`. A resource view sets its non-empty key. In `SkillRead`, `resource=None` asks for the root document.
- **`Resource`** has a key, unique within its document, and a description.
- **`Document`** is a `DocumentView` plus its `subjects`; `view()` drops the subjects for delivery.
- **`SkillSummary`** has `id`, `name`, and `description`. Listings need not include every supplement. A document missing from a role's listing can still be found by `lookup` or read by ID, so listings are not an access control.
- **`MaterialText`** is an immutable `id`, `version`, and `body`. `MaterialText.create(id, body)` sets `version` to the SHA-256 of the UTF-8 body. Keep the ID stable for the same logical text and create a new snapshot when the body changes. Names, bodies, and descriptions each have their own material identity. Models see only the body; recordings keep all three fields.

`FileDocuments` is the built-in implementation. It reads explicitly configured UTF-8 files from `DocumentSource` and `ResourceSource` values, rejects duplicate document IDs or resource keys, and never scans directories. `DocumentSource.auto_roles` limits which roles receive a document automatically. Other providers can use any source format and selection policy.

## Plugging in

Pass `EntryMaterials(documents, settings)` to `sts2_harness.cli.main(argv=None, *, materials=None)` as `materials`. `EntryMaterials` comes from `sts2_harness.entry_materials`, and `SessionMaterials` from `sts2_harness.journal`. `documents` is a factory: it receives a `MaterialContext` and returns the `Documents` provider for one run. See [docs/materials.md](docs/materials.md).

- Builtin `start` and `headless` use the provider. External-driver `start` rejects it with `ConfigurationError`. `check-config` and `journal` ignore it.
- `SessionMaterials` sets `entry_documents_enabled`, `act_guidance: dict[int, tuple[str, ...]]`, and `battle_guidance: tuple[str, ...]`, and is recorded in the session manifest.
- The same provider serves automatic entry guidance, skill reads, and knowledge supplements. Turning off entry guidance leaves the other two working.

## When documents are delivered

**Run context.** When Run enters an act, the core does the following:

- reads the configured act guidance IDs;
- looks up the act itself and every Boss encounter shown on the act map, fetching the map first if needed and checking that the run and act have not changed;
- merges configured and matched documents by ID.

Within an act, new or changed applicable documents are appended incrementally before later Run requests via `Context.append_materials`.

**Combat context.** When a battle starts, the core reads the configured battle guidance IDs. It also looks up the encounter, every card in every pile, relics, potions, all enemies (including inactive ones), player, enemy, and ally powers, and orbs. Selection candidates are included when Combat owns the menu. New or changed battle objects can bring in more material before a later model request. If a referenced object cannot be read, that is a loading failure; the guidance is not silently skipped.

**Delivery rules.**

- The first documents go into the role's system prompt when its context is built.
- Later, `Context.append_materials` appends only documents that are new or changed, comparing the complete view, not just the text. Earlier messages are never rewritten, and an empty selection does not retract anything.
- Each context tracks delivery on its own, even when Run and Combat share documents. A new act context starts a fresh record, so the same material can be delivered again. Pause and resume do not reset it.

## Explicit reads and native knowledge

**Skill reads.** The role's skill tool lists and reads documents. Listings filter `list(role)` by an optional `text` (case-insensitive substring of id, name, or description) and are sorted by id. Pages hold up to 20 entries (`offset` default 0, `limit` 1–20); `total` counts all matches and `next_offset` is `null` at the end. A resource read must name a resource the parent document advertises. Explicit reads always return the complete view, even if it was delivered before, and the context records the result so automatic delivery does not repeat it.

**Knowledge supplements.** The knowledge tool queries the native catalog first. For a lookup, the core passes the native subjects it found to `Documents.lookup` and attaches each returned document to the native entries it lists in `subjects`. Native order and `missing_ids` are kept. Supplements never add native matches, and a missing supplement never makes a native ID missing.

Providers are read-only. Returning action-like text gives a provider no control; actions still go through the role's action tools and normal execution.

## Empty results and errors

| Situation | What to do |
| --- | --- |
| No provider configured, no matches, or no applicable guidance | Return an empty tuple. This is normal. |
| Unknown document ID or unadvertised resource | Raise `UnknownDocument`. |
| An enabled source cannot be read or decoded | Raise `MaterialUnavailable` (or `SupplementUnavailable`). Do not return empty text or disable the source quietly. |

Error messages should name the logical document or resource without exposing private paths or content.

The core handles the consequences:

- An unknown explicit read becomes an invalid-arguments tool result.
- A read or lookup failure becomes `content_unavailable` and counts against the shared tool recovery budget.
- A failure while loading entry guidance pauses the run before its first decision; fix the source and press Start to retry.
- Providers should not add their own retries, fallbacks, or pause logic.

## Recording and replay

The session manifest records `SessionMaterials`. `RecordedDocuments` records every listing, read, and lookup with its result or failure. `DocumentSnapshot` and `MaterialSnapshot` keep the exact names, bodies, resource descriptions, and subjects. Replays use what was recorded, never today's files.

`ReplayDocuments` rebuilds role delivery from the recorded material timeline during offline replay.

To compare alternative material, declare a `MaterialVariant` in `sts2_harness_eval.variants`. It contains:

- `MaterialReplacement(material_id, expected_versions, text)` entries;
- either `HistoricalStart`, which keeps the recorded prefix, or `SceneStart(initial_notes)`, which builds a fresh context at an admitted scene.

Preflight rejects unknown, inconsistent, or undeclared source versions. `VariantDocuments` applies the replacements while keeping document IDs, resource keys, and subjects. The recorded `SessionMaterials` and pre-target game operations are kept. Turning off entry delivery does not stop recorded explicit reads, whose materials can still be replaced. A comparison should report its source settings, IDs, versions, start mode, and execution evidence. See [docs/evaluation.md](docs/evaluation.md).
