# Architecture

This document explains how the core is put together and why. Domain terms are defined in [GLOSSARY.md](../GLOSSARY.md), and key decisions have short records in [docs/adr](adr/).

## What the core does

The core plays Slay the Spire 2 one run at a time, without save/load. It reads the game through the `sts2agent` Mod's public observations, asks a model (or any other decision-maker) what to do, submits the chosen actions through the Mod, and records everything in a local archive.

The repository has three parts:

- `src/sts2_harness`: the Python core described here.
- `src/sts2_harness_eval`: offline replay and evaluation built on the core's recordings. See [evaluation.md](evaluation.md).
- `mod/`: the C# game Mod. It reads native game state, checks legality, performs native operations, and serves them over loopback HTTP. See [mod/README.md](../mod/README.md).

Curated card knowledge, act and Boss guidance, and monster move rules are not part of the core. They live in private document providers that plug in through [EXTENSIONS.md](../EXTENSIONS.md).

## Dependencies

The core runs its own model/tool loop, orchestration, role contexts, and skill delivery. It uses the official OpenAI and Anthropic SDKs through its own adapters for Responses, Chat Completions, and Messages, and does not use agent frameworks or framework skill loaders. General libraries (HTTP, Pydantic validation, and so on) are used for their specific jobs.

## Modules

| Module | Responsibility |
| --- | --- |
| `cli`, `interactive`, `headless`, `live_session` | Startup: attach to the user's game, or launch an isolated developer game instance. |
| `config` | Read one immutable configuration snapshot per process. See [configuration.md](configuration.md). |
| `session` | `RunSession` assembles one run: game access, notes, prompts, documents, model clients, and the flow. |
| `loop` | `DefaultFlow`, the default orchestration of Run, Deck, and Combat. |
| `run`, `deck_role`, `combat` | `RunAgent`, `DeckAgent`, and `CombatAgent` (with `CombatTask` for one battle). |
| `agent` | The shared model/tool loop and capacity compaction. |
| `auto` | Automatic progression: mechanical Run actions that skip a model call. |
| `context` | Each role's conversation, its projection of game state, and its delivery ledger. |
| `capacity` | Input-size estimates and the compaction trigger. |
| `llm` | Protocol adapters, retries, timeouts, and usage reporting. |
| `tools` | Model-facing tool definitions and argument decoding. Definitions live in `tools/definitions.json`. |
| `prompts` | Packaged role system prompts and task rules. |
| `notes`, `route_plan` | Run-scoped strategy notes and the partial route plan. |
| `knowledge`, `materials`, `entry_materials` | Native catalog queries and supplementary documents. |
| `execution` | `Execution.run_unit`, pause handling, action submission, and settlement. |
| `client`, `game` | Typed Mod HTTP client and game operations. |
| `journal`, `model_journal`, `diagnostics` | Recording to SQLite, and bounded diagnostic logs. |
| `external`, `external_http`, `raw` | External driving: the local HTTP tool service for outside agents. |
| `user_input` | Optional live viewer input delivered into the current role's context. |

## How a run flows

`DefaultFlow.run()` (in `loop.py`) is one loop:

1. Get a ready observation from the game.
2. If this is the first act or the native act number changed, create a fresh `RunAgent` and call `enter_act`. The new agent starts from the current public state, the current strategy notes, and the act's guidance. Nothing from the previous act's conversation carries over.
3. If Combat owns the current decision, create a `CombatAgent` for this battle and run a `CombatTask` until the battle ends. The task returns a combat handoff: a program-written report plus the Combat role's review.
4. Otherwise, pick the decision source:
   - a card reward offer or a card-adding selection goes to a `DeckAgent`;
   - everything else goes to the `RunAgent`.
5. Run one execution unit with `Execution.run_unit(source, task)`. A pending combat handoff rides along on the next Run task. If Combat starts again before Run gets a decision, Run receives a handoff-only task first.

A confirmed terminal result (victory or defeat) ends the run straight away. There is no review or other model call after that.

### Execution units and pausing

An execution unit is one complete decision plus its resulting action batch, through settlement and feedback. One automatic-progression action is also one unit. User Pause takes effect between units. Model requests, queries, and notes calls inside a decision are not pause points. See [ADR 0001](adr/0001-shared-decision-execution.md).

`run_unit` checks that a unit may start, gets a ready observation, asks the source for a decision, submits and settles the proposal, and hands the feedback back to the source. The flow chooses the source and task; execution never chooses roles.

Decisions are plain data:

- `ActionProposal` holds ordered action and selection steps. It has no HTTP methods, SDK objects, or tool-call IDs.
- `TaskComplete` ends a task that allows finishing without a game action, such as a handoff-only task or a review.
- Tasks are `ActTask`, `HandoffTask`, and `ReviewTask`.

### Errors and recovery

- **Model errors.** The LLM layer retries connection failures, timeouts, rate limits, and overload up to 25 extra times (configurable). Backoff is 2, 4, 8, 16, then 32 seconds, about 12 minutes in total. Each attempt gets its own full timeout (`LLM_REQUEST_TIMEOUT_S`, default 3600 s). SDK retries are off. Truncated output and non-retryable errors pause the run with context, notes, and game state intact; pressing Start tries again.
- **Tool errors.** Invalid tool calls go back to the model as error results, within a shared recovery budget. Exhausting the budget pauses the run.
- **Combat review failure.** If a review fails, the error is logged and Run gets the report without a review.

## Roles

| Role | Agent lifetime | Context | Tools |
| --- | --- | --- | --- |
| Run | One native act | Rebuilt at each confirmed node change | `run_act`, `run_plan`, `run_next`, `run_notes`, `run_inspect`, `run_knowledge`, `run_skill` |
| Deck | One card offer or card-adding selection | Its own, for that choice | `run_deck_choice` |
| Combat | One battle, including its review | One per battle | `combat_act`, `combat_inspect`, `combat_knowledge`, `combat_skill` |

See [ADR 0004](adr/0004-role-agent-and-context-lifetimes.md) for why the lifetimes are set this way.

**Run** handles routes, rewards, shops, events, and rest sites, and owns the strategy notes. Its agent lasts one act, but its context is rebuilt at every confirmed node change. The fresh context holds:

- the current observation, map, and route plan;
- applicable guidance and the latest battle evidence;
- in ordinary mode, the current notes.

Queries and actions inside one node stay in one conversation, so opening a reward screen or continuing a shop visit keeps the local back-and-forth. The full history stays in the archive.

**Deck** makes one focused card choice: take a card or skip. `DeckAgent` reuses `RunAgent`'s notes, state enrichment, and route-plan view, with its own prompt and only the `run_deck_choice` tool. If an offer is declined in a node, it is not offered to Deck again in that node. The result goes back to Run as progress.

**Combat** plays one battle from the opening state, with a fixed copy of the strategy notes taken at the start. After a nonterminal battle it writes a review in the same context. Run then reads the report and review and may update the notes.

**Automatic progression** (`auto.py`) takes agreed mechanical Run actions without a model call, such as travelling when only one destination exists. Each automatic action is its own execution unit, and the result is still recorded in Run's context. The reasons it shows in game come from fixed text in the current game language. If the language is not supported, automatic progression steps aside and the Run role decides.

**Minimal mode** gives the roles only action and route tools. It skips battle reviews and handoff processing, and has no notes. See [configuration.md](configuration.md#minimal-mode).

### Replacing a role

[`DecisionSource`](../src/sts2_harness/decisions.py#L115) is the decision contract
shared by all three roles. Combat uses it directly. Run and Deck also implement
[`RunRole`](../src/sts2_harness/run.py#L156) and
[`DeckRole`](../src/sts2_harness/deck_role.py#L77), respectively.

```python
class DecisionSource(Protocol):
    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision: ...
    async def accept(self, outcome: ActionToolResult) -> None: ...
```

A fixed-code strategy does not need to inherit `Agent`, build model messages, or configure an LLM. It returns the same `ActionProposal` values and gets the same execution, settlement, reporting, and pause handling. `RunSession` accepts optional factories for Run, Combat, Deck, and the flow. A custom [`RunFlow`](../src/sts2_harness/loop.py#L39) owns card choice and can omit the Deck factory. Replacing Combat does not affect Run, and the reverse is also true. A non-model Combat may return no review; the program report is always produced.

## Context and model input

Each agent owns one `Context`. It is the only record of that agent's current model history and of which content has already been delivered.

**Raw observation and role projection.** The Mod publishes complete, self-contained public facts. Each role's context projects them into its own view. Run's view leaves out combat-internal piles, actors, and previews and keeps Run-owned facts and battle outcomes. External callers get the raw observation. See [ADR 0002](adr/0002-separate-raw-observation-from-role-projection.md).

**Sections and profiles.** State is delivered as complete named sections. When a section changes, the whole section is replaced, not patched field by field. Shared content (card text, power descriptions, and so on) is sent once per context as an immutable profile. Each decision gets a fresh decision surface. See [ADR 0003](adr/0003-project-shared-content-and-complete-sections.md).

**Append-only, with compaction.** Within a context, new content is appended and earlier messages are not rewritten. When estimated input reaches 90% of `LLM_CONTEXT_WINDOW`, `CapacityPolicy` proposes a compacted input. It is a rebuilt current-state baseline plus the latest complete decision group. The agent installs it only if it validates and is actually smaller. Compaction is mechanical and makes no extra model call.

**Protocol state.** Assistant replies keep their complete native payload, including encrypted reasoning and signed thinking blocks, and are sent back unchanged. Role logic only sees normalized text and tool calls. Switching protocols does not migrate an existing history.

**One tool call at a time.** A response with several tool calls is rejected before any of them runs. Each call gets an error result, and the model tries again. One `act` call can still contain many steps.

### Observation policy

- Each role sees the relevant parts of the current public game state, including available actions, HP, cards, and visible map or combat information.
- Run observations include the game's current card rarity offset from `PlayerOdds.CardRarity`. They also include question-mark room probabilities when available. [The Mod](../mod/src/Sts2Mod/Observation/MapObserver.cs) calculates the odds from current weights and room rules without advancing the game's RNG.
- Combat observations keep hand order and sort the other card piles by card reference. The actual draw order stays hidden. The Mod reports both `Hidden` and `Unknown` enemy intents as `unknown`.
- The catalog comes from the running game's definitions via the Mod. Any extra card knowledge or guidance comes from the configured [document provider](../EXTENSIONS.md).

This excerpt is from Run 01, model request sequence 13, in the [Fool A10 archive](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-20-fool-a10/01-control-02). It shows selected fields from the task message. Prompts, knowledge documents, and other fields are omitted.

```json
{
  "task": "act",
  "observation": {
    "card_rarity_offset": -0.05,
    "progress": {
      "act": 1,
      "acts": 3,
      "bosses_left": 1,
      "definition_id": "ACT.OVERGROWTH",
      "floor": 1,
      "name": "密林",
      "position": "map:0:3",
      "room_type": "event"
    },
    "map": {
      "elites_fought": [],
      "unknown_odds": {"elite": 0, "event": 0.85, "monster": 0.1, "shop": 0.03, "treasure": 0.02}
    }
  }
}
```

## Tools

Each role's tool set is fixed when the role is created. The model-visible rules for each tool live in its description in `tools/definitions.json`; gameplay rules live in the role prompts under `prompts/`.

- **`run_act` / `combat_act`** submit ordered steps. Combat has `play_card`, `use_potion`, `discard_potion`, `end_turn`, and `select`. Run uses `interact` for listed operations, plus potion and selection steps. A selection can be planned before its screen opens (an anticipated choice). If the actual options do not allow it, the batch stops there.
- **`run_plan` / `run_next`** set a partial route plan through the current act and travel to the next planned node. Plans are drawn on the in-game map.
- **`run_deck_choice`** takes or skips cards for the current card addition. It is only available to Deck.
- **`run_notes`** reads or writes the strategy notes. `write` replaces only the fields supplied: `core` (up to five distinct entries), `plan` (up to 300 characters), and `other`. Notes can also be written through `run_act` and `run_next`.
- **`*_inspect`** reads public information missing from the current view: `state` and `map`, plus `combat_history` in Combat.
- **`*_knowledge`** queries the native game catalog with `catalog`, `search`, and `lookup` modes, with any configured supplements attached to native matches.
- **`*_skill`** lists and reads configured guidance documents.

### Action results

Every action result has an outcome status:

| Status | Meaning |
| --- | --- |
| `completed` | The submitted steps finished. |
| `rejected` | Nothing ran (invalid arguments, wrong role, busy, or paused). |
| `stopped` | Some steps ran. The result gives the completed steps, the next step, the reason, and suggested next actions. |
| `unknown` | A step may have taken effect but could not be confirmed. |
| `failed` | A query or read failed. |

When the current state cannot be trusted, the result has `state: null`.

### References

Action, card, and potion references are short handles that are valid within the current map node. A card keeps its reference when it moves between piles or its cost changes, and a new object gets a new one. References may restart in the next node. Reviews and cross-node notes name cards by localized name and stable game ID, not by reference.

The Mod resolves references against live objects, checks legality, and runs native operations. Python decodes arguments, enforces role permissions, orders submission, and builds batch feedback. It does not repeat checks the Mod already makes, and it does not simulate game rules.

## Knowledge and guidance

Native game information (names, descriptions, catalog entries) comes from the Mod in the current game language, alongside stable game IDs. Supplementary documents come from a `Documents` provider:

- **Run.** When Run builds a context, it receives the configured act guidance and documents matched to the act's Boss encounters.
- **Combat.** At the start of a battle, Combat receives battle guidance and documents matched to the encounter, cards, relics, potions, enemies, and powers in that fight.
- **Later reads.** Explicit reads and newly relevant material are appended later.

Each context tracks delivery on its own, so unchanged material is not sent twice. See [EXTENSIONS.md](../EXTENSIONS.md) for the provider interface and [materials.md](materials.md) for per-run document factories.

## External driving

With `STS2_DRIVER=external`, the backend runs no built-in agents or automatic progression and needs no model. Outside agents call a local HTTP tool service:

- `GET /tools` returns the role-grouped tool definitions.
- `POST /tools/{name}` returns the complete raw result.

The service allows one call at a time, keeps run-scoped notes, and follows the same pause rules. The [play-sts2](../skills/play-sts2/SKILL.md) skill explains the tools to an outside agent. See [running.md](running.md#external-gameplay-tools).

## Startup

- **Ordinary.** `sts2 start` attaches to the user's running Mod-enabled game. It waits without model calls until the game is reachable, sets Pause, and waits for the in-game Start control. It stays resident across runs, and each run gets fresh notes, contexts, and an archive directory. It never launches or stops the user's game.
- **Developer headless.** `sts2 headless` owns one game process per instance, with its own profile, port, and archive directory. Several instances can run at once. Each instance writes a `status.json` with its paths, port, identities, and safe model settings.

Configuration is loaded once per process. Changing it means restarting that instance. See [running.md](running.md).

## Recording

Every game operation, model request, material read, and control action goes into one SQLite archive per run, written by `Journal`. Archives can be inspected offline with `sts2 journal` and replayed by `sts2_harness_eval` without a model or game. See [library.md](library.md#recordings).
