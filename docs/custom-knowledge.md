# Writing custom knowledge and strategy guides

The core ships without a strategy guide library. It reads native game state and extracts native mechanics from the running game, and lets you plug in your own curated notes—card evaluations, relic synergies, enemy tips, Boss plans, or character guides—to strengthen in-run decisions.

This page is a practical guide to what you can write, when the core delivers it, and how to wire up your own guide folder. For the full provider protocol and error semantics, see [EXTENSIONS.md](../EXTENSIONS.md) and [docs/materials.md](materials.md).

## What you can write

Every guide is plain text (typically Markdown) paired with:

- an **ID** (unique string such as `guide:ironclad` or `boss:queen`);
- optional **`subjects`** (`SubjectId(category, definition_id)`) that bind the document to native game concepts for automatic matching;
- optional **`auto_roles`** (`MaterialRole.RUN`, `MaterialRole.DECK`, `MaterialRole.COMBAT`) controlling which roles receive the document automatically.

Common categories you can target with `SubjectId`:

| What you want to advise on | `category` | Example `definition_id` | Automatically delivered to |
| --- | --- | --- | --- |
| Character drafting / macro strategy | `character` | `CHARACTER.IRONCLAD`, `CHARACTER.FOOL` | Run, Deck |
| Act-level pathing & pacing | `act` | `ACT.OVERGROWTH`, `ACT.HIVE`, `ACT.GLORY` | Run, Deck |
| Boss or Elite encounter gameplan | `encounter` | `ENCOUNTER.QUEEN_BOSS`, `ENCOUNTER.TEST_SUBJECT_BOSS` | Run (upcoming act Bosses & reachable Elites), Deck (same), Combat (active fight) |
| Specific enemy behavior or kill order | `enemy` | `MONSTER.TORCH_HEAD_AMALGAM` | Combat (active or inactive enemies in the fight) |
| Card drafting & play tips | `card` | `CARD.BASH` | Run (visible cards), Deck (deck + offers + selection candidates), Combat (all piles + candidates) |
| Relic or potion synergies | `relic` / `potion` | `RELIC.SOZU`, `POTION.FOUL_POTION` | Deck (owned), Combat (owned) |
| Combat power or orb mechanics | `power` / `orb` | `POWER.VULNERABLE_POWER` | Combat (player, enemy, and ally powers; orb slots) |

You can also attach general act or battle guides by ID—without binding them to a single game subject—using `SessionMaterials(entry_documents_enabled=True, act_guidance={1: (...), 2: (...), 3: (...)}, battle_guidance=(...))`.

> **Tip:** To discover exact native `definition_id` values for any card, relic, potion, enemy, encounter, act, or character, inspect a recorded archive with `uv run sts2 journal --archive runs/<run>/archive.sqlite`, or query the running Mod's catalog endpoint.

## When documents are delivered

Automatic delivery works in **both minimal mode (default) and full mode**:

1. **Run (per act).** When Run enters an act, the core loads configured `act_guidance` IDs for that act and looks up the character, the act, visible Bosses, reachable Elites on the act map, and visible cards. Newly relevant documents are appended incrementally as the act progresses.
2. **Deck (card rewards and card-adding choices).** Deck receives matched guides for the character, the act, upcoming Bosses, reachable Elites ahead on the route/map, and every card, relic, potion, offer, and candidate involved in the choice.
3. **Combat (per battle).** At battle start, Combat loads configured `battle_guidance` IDs and looks up the active encounter, every card across all piles, owned relics and potions, all enemies, active powers, and orb slots. If a new card, enemy, or power appears mid-fight, its matched document is appended before the next model request.

In **full mode (`STS2_MINIMAL=false`)**, models can additionally:

- browse and read any configured document (and its sub-resources) on demand via `run_skill` / `combat_skill`;
- receive subject-matched documents attached as supplements when calling `run_knowledge` / `combat_knowledge`.

Each role context tracks what has already been sent, so unchanged documents are never duplicated inside the same context, preserving prompt-cache prefixes.

## Minimal working example

Create a folder of Markdown files alongside a small launcher script that wraps `sts2_harness.cli.main` using the built-in `FileDocuments` provider:

```text
my-guides/
├── guides/
│   ├── ironclad.md
│   ├── queen-boss.md
│   └── bash.md
└── play_with_guides.py
```

Write short, concrete advice in your Markdown files—for example `guides/queen-boss.md`:

```markdown
# Queen Boss (ENCOUNTER.QUEEN_BOSS)

- Prioritize removing or controlling Torch Head Amalgam before its heavy beam turn.
- Keep enough front-loaded block or burst scaling to avoid getting chipped down while handling minions.
```

Then wire them into `EntryMaterials` in `play_with_guides.py`:

```python
from pathlib import Path
import sys

from sts2_harness.cli import main as harness_main
from sts2_harness.entry_materials import EntryMaterials, MaterialContext
from sts2_harness.journal import SessionMaterials
from sts2_harness.knowledge import MaterialRole, SubjectId
from sts2_harness.materials import DocumentSource, FileDocuments

GUIDES = Path(__file__).parent / "guides"


def build_documents(context: MaterialContext) -> FileDocuments:
    # context.ascension (int | None) and context.minimal (bool) let you
    # pick different files or render custom text per run.
    sources = (
        DocumentSource(
            id="guide:ironclad",
            name="Ironclad Macro Guide",
            description="Act-by-act drafting and pathing priorities for Ironclad.",
            path=GUIDES / "ironclad.md",
            subjects=(SubjectId(category="character", definition_id="CHARACTER.IRONCLAD"),),
            auto_roles=(MaterialRole.RUN, MaterialRole.DECK),
        ),
        DocumentSource(
            id="boss:queen",
            name="Queen Boss Plan",
            description="Preparation and combat priorities for ENCOUNTER.QUEEN_BOSS.",
            path=GUIDES / "queen-boss.md",
            subjects=(SubjectId(category="encounter", definition_id="ENCOUNTER.QUEEN_BOSS"),),
            auto_roles=(MaterialRole.RUN, MaterialRole.DECK, MaterialRole.COMBAT),
        ),
        DocumentSource(
            id="card:bash",
            name="Bash Notes",
            description="Upgrade priority and Vulnerable setup notes for CARD.BASH.",
            path=GUIDES / "bash.md",
            subjects=(SubjectId(category="card", definition_id="CARD.BASH"),),
            auto_roles=(MaterialRole.DECK, MaterialRole.COMBAT),
        ),
    )
    return FileDocuments(sources)


def main() -> int:
    materials = EntryMaterials(
        documents=build_documents,
        settings=SessionMaterials(
            entry_documents_enabled=True,
            act_guidance={},
            battle_guidance=(),
        ),
    )
    return harness_main(sys.argv[1:], materials=materials)


if __name__ == "__main__":
    raise SystemExit(main())
```

Run your launcher with the exact same CLI arguments as `sts2`:

```console
uv run python my-guides/play_with_guides.py start
```

> **Note:** `EntryMaterials` requires built-in driving (`STS2_DRIVER=builtin`). Passing `materials` with `STS2_DRIVER=external` raises a configuration error.

## Authoring tips

- **Focus on strategic judgment, not raw numbers.** The Mod already extracts live card text, per-target previews, enemy move rules, and upgrade diffs directly from game code. Use your documents for drafting tradeoffs, synergy breakpoints, upgrade priority, and fight plans rather than restating stats that change across patches.
- **Keep documents concise and scoped.** Set `auto_roles` tightly (for example, `RUN` + `DECK` for macro character guides, `COMBAT` for tactical enemy notes) so each role only receives material relevant to its decisions.
- **Reference concepts with stable IDs alongside names.** Including native IDs like `CARD.BASH` or `ENCOUNTER.QUEEN_BOSS` keeps your notes unambiguous regardless of the game's display language.
- **Dynamic per-run providers.** If you want to template guides by Ascension level or store notes in YAML/JSON/SQLite instead of static files, implement the three-method `Documents` protocol (`list`, `read`, `lookup`) and return rendered `MaterialText` snapshots. See [EXTENSIONS.md](../EXTENSIONS.md) and [docs/materials.md](materials.md).
- **Verify changes offline.** Every delivered document snapshot is recorded in `archive.sqlite`. You can compare guide revisions against recorded decisions using `MaterialVariant` in `sts2_harness_eval` before running full live games; see [docs/evaluation.md](evaluation.md).
