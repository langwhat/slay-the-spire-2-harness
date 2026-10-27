---
name: play-sts2
description: Play Slay the Spire 2 through a running STS2 Harness external HTTP backend, using Run and Combat tools.
---

Connect to the backend's local tool endpoint, normally `http://127.0.0.1:17878`.
Read [Run tools and strategy](references/run.md) before run decisions and
[Combat tools and strategy](references/combat.md) before battle decisions. Follow the
returned phase when choosing the next role. Both references describe their role's
public game facts, action rules, and strategy.

`GET /tools` groups definitions under `roles.run` and `roles.combat`; each contains
`name`, `description`, and `input_schema`. Use the configured endpoint if the host supplies
a different port.

## Connection and control

Use the Python backend started in external mode. GET /tools discovers the role tool names, call descriptions, and self-contained input schemas. POST /tools/{name} invokes a tool; its JSON body is the argument object. Read raw result semantics here, not in a built-in role prompt. Each tool call returns its final result; it does not start a background job for the caller to poll.

The backend runs no built-in gameplay model or automatic progression in external mode. You decide which offered operations to perform, including routine progression. Read knowledge and skill material explicitly when needed; raw observations do not automatically inject private guidance. Notes remain specific to the current run. You decide whether to review a battle and update notes; the backend does not request a model review or handoff.

Pause takes effect after an already accepted tool call finishes, including all valid steps of its action batch. While paused, new game actions and notes writes are refused; read-only queries remain available. Resume belongs to the host or game controls, not a model tool. If the host pauses while you are thinking, your next action is refused. An uncertain result is not permission to repeat an earlier submission.

Send one tool call at a time and wait for its result before sending another. The same backend instance rejects concurrent tool calls, including reads, with reason code busy and state=null. Static tool discovery remains available. A busy response means the new call was not accepted; wait before inspecting again.

A client disconnect or response timeout does not cancel an accepted operation. The backend continues until its final outcome. After reconnecting, inspect the current state once the backend is no longer busy; do not resubmit an earlier action because its response was lost.

## Reading results

Each result contains the current short state and complete relevant object and description directories. `generated` and `mechanisms` contain the current complete directly related previews and explanations. Source references belong to the current map node. A preview's card definition is not an actionable instance. Refresh the current state before submitting actions after reconnecting or after the game's node or phase has changed.

Outcome status is completed when the submitted operation finishes; rejected when a request is not accepted at entry and nothing is executed, including invalid arguments, permission restrictions, busy, or paused; stopped when ordered execution ends with a known completed prefix; unknown when an operation may have taken effect but its result cannot be confirmed; or failed when a query or read fails. An uncertain notes write is unknown, not failed. A stopped action result gives completed_steps, next_step (one-based), reason with code and message, and suggested_actions. An unknown action result gives confirmed_steps and uncertain_step. No trustworthy current observation means state=null; do not treat an earlier observation as current.

Each potion reference identifies one actual bottle in the current map node. Slot movement does not change it. A consumed or discarded bottle loses its reference; a newly acquired bottle receives a new reference rather than reusing a retired one in that node. References may restart in the next node.

`outcome` reports the submitted operation's result. A non-null `state` contains the complete compact current public observation. The `objects` and `descriptions` directories supply complete relevant live object details and mechanism descriptions for this result; read the state and directories together. Card descriptions are keyed directly by the card reference. Card descriptions are the current native card text without a specified target and may include current modifiers. Use the latest complete text for each card reference. A numeric change may change that text; read the complete current text in this result. Object previews contain {target, text} entries with the native text for the specified target at this observation. They do not promise later effects or final HP loss. Do not apply an already included modifier again; predict changes caused by earlier steps and consumed effects. A missing target preview does not establish target illegality. These directories do not depend on earlier observations. Use this result for current facts. Prior observations remain useful as history, not as substitutes for missing current values. `state: null` supplies no trustworthy current observation. Query results appear in `data`. Native definition values and hypothetical upgrades do not override live values. A null dynamic value is unknown now, not zero; do not substitute an older value. A power without a numeric counter has no amount field; amount=null means its amount is unknown, not that the effect is absent. Live query details describe that observation and may be outdated after later changes. Read the returned phase after any transition.

Current previews apply to the observed state and shown target. Predict later steps using the actual effects, counters, and triggers; a one-use bonus is not available for every card just because every starting preview includes it. Read the act tool's stopping and recovery rules before continuing a failed or uncertain sequence.

Card entries include current card_type, card_pool, rarity, integer upgrades, is_upgradable, and is_removable. These are current facts, not promises of eligibility after earlier actions. keywords is the complete current keyword list. local_keywords, when different, describes card-local keywords including local modifications; it is not the printed base version. In a complete entry, omitted local_keywords means it equals keywords, not that an older local list remains active. Unknown values remain unknown. should_retain_this_turn, exhaust_on_next_play, and has_turn_end_in_hand_effect describe current native behavior; read the supplied text for the actual effects and amounts. Do not treat temporary flags as permanent properties.

The enchantment and affliction fields describe modifications to this particular card. Their short entries give native identity, name, amount, and applicable native status; null means that modification is absent. The complete modifier_descriptions directory, keyed by card reference and modification type, supplies their current native text. These descriptions do not introduce additional actionable references or replace the main card description.

Relevant native keyword explanations and directly referenced card previews accompany the current public details. A referenced card preview describes public rules, not a card already present in a pile. Only a card actually present with a supplied instance reference can be used in an action. Related previews expand one level; do not infer unavailable details or a complete recursively expanded catalog.

The nonempty cost object lists applicable resources. Each value is a nonnegative numeric cost, "X", "none" for no consumption of that resource, or null for an unknown cost. For example, cost={"energy":2,"stars":1} spends both resources; cost={"energy":0} has zero energy cost, while cost={"energy":"none"} does not consume energy. An omitted resource is not applicable. Playability is separate from cost.

When supplied, cost_without_global_effects gives the current cost without global modifiers, retaining card-local modifiers. It uses resource keys and is included only for confirmed values that differ from the displayed cost. It is not the printed base cost or a promise of a later step's cost. Do not assume missing resource values are available.

A non-null selection gives the current prompt, source, min, max, complete candidates, and selected references; selected=[] means nothing is currently selected. min=0 permits declining the selection with a select step whose refs is []; min>0 requires at least that many choices. An empty selection submission completes the choice without taking an option; it is not a cancellation of prior game actions. Use the native count constraints, not the prompt wording alone. selection=null means no choice is open.

An explicit upgrade inspection returns the current live details and a separate data.upgrades entry keyed by the card reference, containing the complete hypothetical next-upgrade version. Compare those entries directly; no separate before copy or numeric difference is supplied. Not upgradable and unavailable preview are distinct. A read failure is reported as a failure, not hidden as missing data. Hypothetical values never replace current live values.

Card rewards, shop card offers, and choices whose native purpose is upgrading automatically include the next-upgrade preview. Other cards require an explicit upgrade inspection. Relevant automatic previews are included in the complete public upgrades directory, keyed by card reference, with their own complete hypothetical text. They never replace live card values. An explicit inspection still returns the requested content in full.
