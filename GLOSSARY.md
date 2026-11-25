# Glossary

Terms for game actions, role decisions, strategy handoffs, and knowledge used during a run. How the modules fit together is in [docs/architecture.md](docs/architecture.md).

## Game objects and references

**Stable game ID**:
A language-independent identifier the game defines for a concept, such as a card type or a power. Every instance of that concept shares it.

**Game instance**:
One occurrence of a game concept, such as one particular card in the deck or one enemy in a fight.

**Card reference**:
A short handle for one actual card in the current map node. It follows the card through pile moves and cost or effect changes, and may be reused for another card only after the node changes. It is not a card-definition ID or a menu position. Reviews and cross-node notes name cards by localized name, stable game ID, and any distinguishing properties instead.

**Action reference**:
A handle for one game operation in the current node, bound to what it does rather than where it sits in a menu. Objects and targets have their own references.
_Avoid_: Action index, menu position

## Roles and orchestration

**Run role**:
Makes overall run strategy and non-combat choices: routes, rewards, shops, events, and rest sites. It reads combat outcomes and uses them to guide later fights.

**Deck role**:
Plans the node's visible card reward groups jointly (or resolves a standalone card-adding selection) for the Run side: choosing which offered cards to take and which groups to skip.

**Combat role**:
Makes the actions and choices inside a fight.

**Role implementation**:
Anything that fills the Run, Deck, or Combat role. It can be a model-based agent or plain code.

**Run orchestration**:
Coordinates role decisions, automatic progression, combat handoffs, and context changes across a run.

**Automatic progression (auto)**:
Mechanical non-combat actions the core takes without a model call, under agreed conditions. The same operations stay available through the model's tools, and automatic results are still recorded in the relevant role context.

**Combat task**:
One battle within a run, from its opening state through its outcome and any review. Its result gives the Run role the observed facts and findings from the fight.

## Decisions and execution

**Decision task**:
The job handed to a decision-maker, with a clear completion condition. Most tasks need a game action; a handoff-only task can finish without one.

**Action proposal**:
A request for a game action, choice, or batch, from any source: a model, plain code, or automatic progression. A proposal does not mean the game has performed it.

**Execution unit**:
One complete role decision and its action batch, or one automatic action, through settlement and feedback. User Pause takes effect between units. A unit is a pause boundary, not an all-or-nothing transaction.

**Task completion**:
An explicit successful end to a task that allows finishing without a game action. It is distinct from proposing an action or asking to observe again.

**Anticipated choice**:
A selection planned before its choice screen appears, based on public information. It is checked against the real options when that step is reached.

**Route plan**:
A partial plan of future map paths in the current act, possibly with alternatives beyond the next destination, including special movement such as cross-branch flight. It states intended travel; it does not perform actions.
_Avoid_: Action batch

**Map annotation**:
A visible marking of a submitted route plan on the in-game map. Annotations can pile up across replans and show old intentions; they are not the active plan or proof that a move is legal.

## Observations and context

**Public observation**:
Information the game provides about the current state, an action result, or instance details. Seeing the same observation twice does not mean a new action or turn happened, and a partial detail does not mean the omitted information is gone.

**Raw observation**:
A self-contained public observation before any role-specific projection. It is semantic plain text with all the public facts a caller needs without a role context.
_Avoid_: Role projection, model context

**Role projection**:
A role's view of a raw observation, delivered into its context. It contains only what that role owns or needs, and may refer to unchanged content already delivered in the same context.
_Avoid_: Raw observation, public observation

**Role context**:
The conversation and delivery record owned by one role agent. A Run context covers one native act inside an act-scoped Run agent. A Deck context covers a node's visible card reward groups (or a card-adding selection) across any re-decisions until they resolve or the node changes. A Combat context covers one battle through its review. It holds dialogue, projections, handoffs, and delivered knowledge. Another role's history and archived Journal evidence are not part of it.

**Projection profile**:
An immutable description of observed game content, shared by instances with identical content within one role context. It is neither a native game definition nor a single game instance.

**Projection catalog**:
All the projection profiles delivered in one role context. It holds observed content, not the game's searchable card catalog.

**Decision surface**:
The current choices and live facts given for one decision, including costs, targets, and previews. It is not authoritative for any later decision.

## Strategy and handoffs

**Strategy notes**:
Short, current strategic guidance kept by the Run role for this run. It carries plans, priorities, and lessons between roles; resolved or outdated items are removed.

**Opening strategy**:
The copy of the strategy notes handed to the Combat role at the start of a fight.

**Combat report**:
A program-written account of what was observed in a fight.

**Combat review**:
The Combat role's assessment of a finished nonterminal fight, written from its retained context and the report. It covers tactical outcomes and findings for the Run role to weigh.

**Combat handoff**:
Passing the combat report and any review to the Run role before the next Combat decision. Run may keep the notes as they are.

## Knowledge

**Native card information**:
The game's own card information, including names and descriptions in the current game language.

**Card catalog**:
The game's searchable collection of public card definitions, including cards outside the current deck or fight.

**Custom knowledge**:
Supplementary game information supplied by the user, separate from the game's native information. It may explain mechanics or give strategy advice.

**Skill**:
Task-oriented guidance on what to consider and how to use relevant knowledge, with its associated resources. A skill may contain or share knowledge documents; it is not a decision-making role.

**Monster knowledge**:
Supplementary information on a monster's moves, passives, and behavior-transition rules, separate from observations of a particular monster.

**Monster state-machine knowledge**:
A monster's move sequences, transition conditions, and possible branches. It describes the rules, not the current state of a particular monster.

**Encounter knowledge**:
Supplementary information on an encounter's composition and conditions, separate from the current fight's observed participants and state.

**Act guidance**:
Knowledge for planning an act ahead of time: possible events, encounters, and rewards, with their conditions and probabilities, and what they mean for card picks, routes, and resources.

**Knowledge delivery**:
Putting knowledge content into a role's current context. Content that sits only in a cache, an archive, or another role's context has not been delivered to that role.

**Game extension**:
An optional source of supplementary knowledge, guidance, or interpretations of observed state. It may keep observations and caches; game actions stay under core control.
