# Game interaction

Terms for game actions, role decisions, strategy handoffs, and knowledge used during a run. Module responsibilities are documented in [Architecture](docs/architecture.md).

## Language

**Stable game ID**:
A language-independent identifier defined by the game for a game concept, such as a card type or power. Multiple instances of that concept share this identity.

**Game instance**:
An individual occurrence of a game concept, such as one particular card in a deck or one enemy in a combat.

**Card reference**:
A short identifier for one actual card within the current map node. It remains attached to that card through pile movement and cost or effect changes, and may be reused for another card only after changing nodes. It is distinct from a native card-definition ID or a temporary menu position. Reviews and cross-node notes identify cards by name, native definition ID, and relevant distinguishing properties instead.

**Action reference**:
A supplied handle for a particular game operation in the current node, bound to its meaning rather than its position in a menu. Object and target references identify their actual instances separately.
_Avoid_: Action index, menu position

**Run role**:
The decision-making role for overall run strategy and non-combat choices, including routes, rewards, shops, and events. It interprets combat outcomes and findings to guide later combats.

**Combat role**:
The decision-making role for actions and choices within a combat.

**Combat task**:
One battle undertaken within a run, from its opening state through its outcome and any applicable review. Its result supplies the Run role with observed combat facts and available findings.

**Automatic progression (auto)**:
Non-combat game actions selected under explicitly agreed conditions to save model calls. The same game operations remain available through model tools; automatic progression results remain part of the relevant role context.

**Role implementation**:
A replaceable decision-maker that fulfills the Run or Combat role. It may be a model-based agent or another implementation without a model.

**Run orchestration**:
The coordination of role decisions, automatic progression, combat handoffs, and context maintenance throughout a run.

**Route plan**:
A partial plan of possible future map paths in the current act, including special movement such as cross-branch flight, which may retain alternatives beyond a unique next destination. It expresses intended travel rather than performing game actions.
_Avoid_: Action batch

**Map annotation**:
A visible map marking of a submitted route plan. Annotations may accumulate across replanning and show superseded intentions; they are not the active route plan or proof that a move is legal.

**Action proposal**:
A source-independent request for a game action, choice, or batch, selected by a model-based role, a non-model strategy, or automatic progression. A proposal does not establish that the game has performed it.

**Decision task**:
The specific game-related responsibility assigned to a decision-maker, with a defined completion condition. Some tasks require a game interaction; a handoff-only task can complete without one.

**Execution unit**:
One complete role decision and its resulting action batch, or one automatic-progression action, through settlement and feedback. User-requested pausing takes effect between these units; a unit does not imply all-or-nothing game changes.

**Task completion**:
An explicit successful end to a task that permits finishing without a game action. It is distinct from an action proposal or a request to observe the game again.

**Anticipated choice**:
A selection included in an action plan before its choice interface appears, based on known or inferred public information. Its validity is determined against the actual available options when that step is reached.

**Public observation**:
Game-provided information about the current state, an action result, or public instance details. Repeated observations do not by themselves establish a new action or turn, and partial details do not establish that omitted information has disappeared.

**Raw observation**:
A self-contained public observation published before role-specific projection. It uses semantic plain text and retains the public facts needed by callers that do not share a role context.
_Avoid_: Role projection, model context

**Role projection**:
The role-specific view of a raw observation delivered into one role context. It contains only facts owned or needed by that role and may refer to unchanged content already delivered within that context.
_Avoid_: Raw observation, public observation

**Role context**:
The conversation and delivery record owned internally by one Run or Combat agent for its lifetime: one native act for Run, or one battle through nonterminal review for Combat, with terminal battles ending directly. A confirmed native act transition replaces the Run agent; Boss reward completion does not determine its lifetime. It includes dialogue, role projections, handoffs, and delivered knowledge; another role's history and archived Journal evidence are not part of this context.

**Projection profile**:
An immutable description of observed game content shared by instances with identical semantic content within one role context. A profile is distinct from both a native game definition and an individual game instance.

**Projection catalog**:
The collection of projection profiles delivered within one role context. It contains observed content, rather than the game's searchable card catalog.

**Decision surface**:
The current choices and live facts supplied for a particular role decision, including applicable costs, targets, and previews. It does not remain authoritative for a later decision.

**Strategy notes**:
Concise, current strategic guidance maintained by the Run role for the current run. It carries active plans, priorities, and relevant lessons between roles, with resolved or superseded guidance removed.

**Opening strategy**:
The fixed version of the strategy notes given to the Combat role at the start of a combat.

**Combat report**:
A program-produced account of observed facts from a combat.

**Combat review**:
The Combat role's assessment of a completed nonterminal combat using its final retained context and report, including tactical outcomes and findings for the Run role to evaluate. Discarded history is not restored for this assessment.

**Combat handoff**:
The transfer of a combat report and any available combat review to the Run role for processing before the next Combat decision. The Run role may retain the existing strategy without changing the notes.

**Native card information**:
The game's own card information, including names and descriptions in the current game language.

**Card catalog**:
The game's collection of public card definitions, searchable by card properties. It includes cards outside the current deck or combat.

**Custom knowledge**:
Supplementary game information supplied by the user, separate from the game's native information. It may include explanations or strategic guidance.

**Skill**:
Task-oriented guidance about what to consider and how to use relevant knowledge when making game decisions, together with its associated resources. A skill may contain or share knowledge documents; it is not a separate decision-making role.

**Monster knowledge**:
Supplementary information describing a monster's moves, passives, and behavior-transition rules, separate from observations of a particular monster instance.

**Act guidance**:
Knowledge supporting advance planning for an act's possible events, encounters, and rewards, including conditions, supported probabilities, and implications for card selection, routes, and resource preparation.

**Monster state-machine knowledge**:
Information describing a monster's move sequences, transition conditions, and possible branches. It describes behavior rules rather than the inferred current state of a particular monster instance.

**Encounter knowledge**:
Supplementary information describing an encounter's composition and conditions, separate from the current combat's observed participants and state.

**Knowledge delivery**:
The inclusion of knowledge content in a particular role's current context. Content retained only in a source cache, an archive, or another role's context is not delivered knowledge for that role.

**Game extension**:
An optional source of supplementary knowledge, guidance, or interpretations of observed game state. An extension may retain observations and caches while game actions remain under core control.
