---
name: play-sts2
description: Play Slay the Spire 2 through a running STS2 Harness external HTTP backend, using a compact minimal-delta CLI helper with deterministic auto-advance.
---

Connect to the external HTTP tool backend, normally `http://127.0.0.1:17878` (or the port configured by the host). Discover tool definitions with `GET /tools` (grouped under `roles.run` and `roles.combat`) and invoke tools with `POST /tools/{name}`.

## Compact minimal-delta CLI helper (strongly recommended)

Raw `POST /tools/*` JSON replies average 30–60 KB because every reply repeats all card descriptions, relics, deck cards, map nodes, and mechanisms. **Always use [`scripts/sts2_cli.py`](scripts/sts2_cli.py) instead of dumping raw `curl` JSON into model context.** It uses only the Python 3 standard library and preserves every actionable `ref`, live number, preview, modifier, and choice:

```sh
CLI="python3 /path/to/play-sts2/scripts/sts2_cli.py --url http://127.0.0.1:17878 --session game --auto"
$CLI state run
$CLI act run '[{"kind":"interact","action":"..."}]' --reason "..."
$CLI act combat '[{"kind":"play_card","card":"...","target":"..."},{"kind":"end_turn"}]' --reason "..."
$CLI notes write '{"core":[...],"plan":"..."}'
$CLI call run_knowledge '{"mode":"lookup","ids":["CARD...."]}'
```

- **Minimal-delta output (`--session <id>`):** Omits unreachable map moves (`enabled: false` with `destination`), deduplicates unchanged map topology across an act (always showing `legal_destinations`), suppresses unchanged relics/potions/non-hand combat piles, and prints permanent deck changes as deltas after the first snapshot.
- **Deterministic zero-branch auto-advance (`--auto`):** Automatically executes strictly mechanical Run steps after a completed call (claiming battle gold/relics and potions when slots are free, opening a sole remaining card reward, opening an unvisited shop entrance, opening chests / claiming unambiguous chest relics, clicking a sole `proceed`, and traveling a sole legal map destination), reporting what it did in `AUTO_ADVANCED:` and stopping at the next real decision or battle Turn 1. Never makes strategic choices.
- **Self-controlled full refresh (`--full`):** Whenever your conversation context is compacted—or you need to re-verify full deck, relics, potions, pile contents, or rules—run any command (e.g., `state run` or `state combat`) with `--full`. It prints a complete compact snapshot **and resets that `--session`'s delta baseline** so subsequent calls resume minimal-delta mode. Use `map <role>` to inspect full map topology on demand, or `--raw` for raw JSON.
- **Minimize extra model turns:** Game `act` animations take ~3–10 seconds; set your shell command synchronous wait timeout to at least 10 seconds when calling `act` to avoid extra background-wait turns. Batch all deterministic actions (including `end_turn` in combat or multi-step shop/reward actions in run) into a single `act` call whenever no unrevealed draw/random result intervenes.

## Execution architecture (choose lowest overhead for your harness)

1. **Direct single-agent with `--auto` + minimal delta (lowest token cost):**
   - Read [Run and Deck guide](references/run.md), [Combat guide](references/combat.md), and [Wire protocol](references/protocol.md) once, then drive both `phase == "run"` and `phase == "combat"` directly in one conversation using `--session game --auto`.
   - Eliminates subagent cold-start overhead, coordinator relay turns, and redundant `state run`/`state combat` handoff calls.
   - Keep responses concise (tool call only during routine turns; no long narration). If context grows large or gets compacted by the harness, run `--full state run` (or `--full state combat`) to reset the delta baseline cleanly.
2. **Optional bounded dual-worker delegation (for harnesses without context compaction on long runs):**
   - If a single conversation would hit context limits without compaction, pair a Run+Deck worker (`--session run-<k> --auto --handoff`) with a reusable Combat worker (`--session combat-<k> --handoff`), rotating each worker when `OUTCOME:` reports `ROTATE_RECOMMENDED` (`session_calls >= 24`), on an Act change, or after a Boss. Keep strict single-flight (never run both workers concurrently).

## Connection, control, and recovery

- **External mode scope:** The backend runs no built-in gameplay model, automatic progression (use `--auto` in `sts2_cli.py` client-side instead), or automatic guidance injection.
- **Synchronous single-flight calls:** Each `POST /tools/{name}` returns its final outcome. Concurrent `POST`s (even reads) return `busy` with `state: null`. `GET /tools` is always available.
- **Pause and Start:** Newly attached games start paused until Start is pressed. Pause takes effect after any accepted call finishes. While paused, actions and `run_notes` writes return `control_unavailable`; reads still work.
- **Transitions and disconnects:** After connecting, a new run, or a node/owner change, refresh with `state run` or `state combat` before writing (`reference_expired` otherwise). Disconnects do not cancel accepted calls; wait until not `busy`, inspect state, and never blindly replay an `unknown` or lost action.
- **External tool caveats:** `run_notes` uses structured fields (`core`, `plan`, `other`); do **not** send `summary` (rejected with `wrong_role`). `run_skill` and `combat_skill` have no documents configured in external mode (always empty)—skip calling them.
