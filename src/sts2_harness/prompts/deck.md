# Deck

A deck change is judged by what the deck can do in the fights ahead, worked out from the cards' rules as shown and how they play together over a fight, in numbers rather than printed values or habit. Card offers are separate decisions: decide each after the earlier ones have changed the deck.

Supplied knowledge, when present, adds exact rules for owned and offered objects, the HP, powers, and intents of the act's Elites and Boss and of the final act's Bosses, and cores known for this character. A core it lists is one candidate with conditions, not an order; combinations it does not list are found the same way, from the cards' text.

## What the deck must answer

Bosses decide the run. Later acts' Bosses have far more HP and hit hard on most turns, and the final act may require two back to back with no recovery between. A deck that wins them meets four demands:

- Early damage: enough to end first-act fights and Elites before their damage piles up.
- Damage to several enemies: single-target damage lowers the sum of their attacks one enemy at a time.
- Block that keeps up with the largest hits, or prevention that scales with them, such as weakening the enemy. Name the turn it is missing on: the opening, the turns spent setting up, or a later large hit.
- Growth: damage per turn that keeps rising during the fight, so a high-HP Boss dies before its damage outgrows the deck's block. Cards played at their printed values give roughly the same damage every turn, and a few energy of them falls well short of later Bosses' HP.

Relics count toward the demands. A relic that blocks, prevents damage, gives energy or draw, or adds a stat or an event every turn does that job without a draw or energy.

## Reading the fights

`fights` lists this run's most recent battles, oldest first: floor, room kind, turns, and HP on entering and on leaving, which includes healing granted when the battle ends. A fight that took far more turns or HP than comparable fights is evidence of a gap, not proof of its cause:

- A late kill turn: the main cards arrive late because the deck is large or draws little, or they do not grow fast enough once in play.
- HP lost although the fight ended on time: block is short on the turns the enemy hits hardest.

`use.observed_plays` counts each owned card definition's plays in the sampled recent battles. Chosen cards with few plays while basic cards are still played show hands that hold more than they can play, or a deck too large for its cards to come around.

`combat_review`, when present, is Combat's account of the latest battle: when the key cards were drawn and played, what limited them, and what cost HP. It is one fight's evidence, shaped by that fight's draws and enemy. Use it to check the picture above, and weigh it more when `fights` or earlier battles point the same way. A gap that only this fight shows is a hypothesis, and a remedy for this one enemy is a weak ground for a card that will be drawn in every later fight.

When supplied knowledge describes the act's Elites and Boss or the final act's Bosses, play those fights out with the current deck, relics, and potions: on which turn the core is likely drawn and running, what it then deals each turn, and how much of the enemy's damage the hands before that can block. Without it, use comparable fights to estimate a longer, harder Boss fight. When history is empty or incomparable, say what is unknown; do not invent enemy HP, damage, or kill turns.

## What a deck is made of

- Core: a small linked group that makes the fight's output rise. Later acts are usually won by the core, because growth is what keeps up with their Bosses.
- Support: energy, draw, and block. The core helps only once it is drawn and paid for; in a large deck that comes later and less reliably. Draw and cycling find the core sooner; in a large deck, a card that fetches a chosen card or puts it on top of the draw pile does this more reliably than plain draw. Energy pays for setting it up alongside the turn's other plays, and block or prevention keeps the player alive until it pays off. Against a Boss that hits hard every turn, a core that comes online a few turns late can fail to come online at all.
- Transition: a card whose own numbers are strong and that needs no other card. It raises a starting deck at once and carries the first act. Its value is the same on every draw and does not grow; once the deck has a direction, each draw of it is a draw that was not the core or its support.

### Reading combinations from card text

Read every owned card, relic, and offered card for what it produces and what it responds to:

- Sources produce something that accumulates or recurs during a fight: a stat that stacks, a card that grows each time it is played, a power whose effect stacks every turn, or a stream of events such as HP lost, cards exhausted, drawn, discarded or generated, attacks played, block gained, debuffs applied.
- Payoffs respond to a source: "whenever", "for each", "at the start of your turn", a bonus that counts a stat, a pile, or cards with a certain name or type. A payoff that fires per event is worth as many events as the deck produces in a turn; one that fires per turn is worth the turns left after it is played.
- Multipliers raise what other cards already do: a stat added to every hit, a card played again or copied, extra damage against a debuffed enemy, energy or cost reduction for a kind of card. They pay in proportion to how many matching cards the deck plays, and how they pay depends on how the deck hits: a bonus per hit favors many small hits, a percentage favors large ones.

A link exists when one card's output, including a side effect presented as a cost such as losing HP or exhausting a card, is exactly what another card or relic counts or reacts to. A source that grows by itself, such as a stat gained every turn or a card that strengthens each time it is played, can carry a deck alone. A power that repeats the same effect each turn does not grow; it is worth that effect over the turns left after it is played, less what it costs each turn. A payoff is worth the events the deck already produces in an ordinary fight; when its own text is the main supplier, judge it by that part alone. Once the deck already produces plenty of a source's events, another card that only adds more of them adds little; what the group then lacks is usually more payoff, or a way to draw it sooner. A group with only one half, such as draw or debuffs with nothing that grows from them, only multiplies the plain cards already played.

When an offer links to owned cards or relics, picture the Boss fight a few turns after the group is in play and compare the deck's damage per turn then with and without the candidate. A group whose output keeps rising outweighs a card with a larger printed number. A missing half that this character's rewards offer routinely is worth taking before the other half is complete; one that needs several more pieces, or a rare one, is a plan, not yet a card.

The core is the current best answer, not a commitment. When an offer would form a stronger group with owned cards, the new group becomes the core and the old group's cards become transition cards. A card that is neither a source, a payoff, a multiplier, nor support is rarely a reason to change direction.

## How cards reach a turn

A turn draws `base_draw` cards and plays what its energy pays for. With N circulating cards (`deck_stats.circulating`), each card is in hand about once every N/`base_draw` turns and is first drawn on turn (N/`base_draw` + 1)/2 on average, so every added card delays the core and its support a little, in every later fight. The delay is small for one card and adds up: an offer nearly always holds a card that is better than a basic one, and a deck that takes those offers grows large and slow, with its core arriving late against the Bosses that matter most.

- A card that does what an owned card already does is weighed against the best owned card of that job, not the weakest. In a hand holding both, energy usually plays the better one, so the weaker card is played mainly in hands that lack the better one, while it makes the better one and the core arrive less often. A card no better than the best of its job is a worse copy, and upgrading the owned one usually does more.
- A power or setup card costs its draw and energy once per fight and then acts on every later turn; count the turns left after it is likely played, and any immediate benefit.
- Energy, 0-cost cards, and draw that nets cards enlarge the turn instead of competing for it. Where hands already hold more than energy pays for, energy adds plays and draw adds choice; where energy goes unused, draw adds plays.
- Cards that work only when played together share a hand far less often than each is drawn, unless something holds, fetches, or replays them.
- A card with a condition is worth its numbers on the turns the condition holds. Count what the deck already supplies toward it in an ordinary fight, including relics.
- A second copy competes for the same turns as the first, and adds little while the first copy's effect still lasts. Upgrades and removals are few, so one strong card for a job, upgraded, often does more than several.
- Cards that enemies add during a fight take a larger share of a smaller deck. A small deck is not a problem in itself; a small deck without strong numbers or growth is.
- A character with base_orb_slots 0 gains one orb slot when it channels an orb while its current orb capacity is 0; channeling into a full, nonempty queue first evokes its front orb.
- Removing a weak card, or upgrading an owned one, improves every later hand and adds no card.

## The decision

1. From the deck alone, before weighing any candidate: how its damage grows against the next Boss, which cards that depends on, roughly when they are drawn and running, and what holds it back most: no growth yet, the core arriving late (size, draw), energy for setup, or block on the hardest turns.
2. For each candidate, read its text against the owned cards and relics: which of them it feeds or is fed by, and what that group does a few turns after it comes together; or whether it brings the core in sooner, pays for it, or keeps the player alive until it lands. A card with none of these adds only its printed numbers, measured against the best owned card of its job.
3. Lean toward skipping. Skipping keeps every owned card arriving as often as now, and later offers, removals, and upgrades remain. A candidate that starts, completes, or multiplies the deck's growth, or that eases what step 1 found holding it back, is the usual reason to take a card. When any number of cards may be chosen, place each after the ones already chosen; choosing none is valid.

The reason names the owned cards or relics the candidate links to and what the group does over a fight, or the limit from step 1 it eases; a card taken for its printed numbers says so. A skip names what the deck already has or why the card would slow it.

The act changes what is worth a draw:

- First act: hands are mostly basic cards, and the deck must first beat this act's Elites and Boss. Strong transition cards, damage to several enemies, and block for the hits the fights show are worth taking now. Take sources, payoffs, energy, and draw when they appear; they are offered less often than damage, and two or three starts can be owned at once.
- Later acts: hands hold chosen cards and the deck should have a direction. Take the missing half of the core, a multiplier for it, energy or draw that brings it in sooner, and block for the Boss's hardest turns. A card that beats a basic card but not the best owned card of its job is usually a skip now, because it is drawn in place of the core for the rest of the run.
- Final act: few fights are left in which a new card is drawn. Take a card when it changes how the remaining Boss fights go; spare gold is not a ground, since a bought card still costs its draw in every hand left. Removal and upgrades often do more.

## Choices that change the piles

When the choice generates, retains, removes, or replaces cards:

- By the critical turn, what is the net change to the piles after exhaustion and continued generation? What does a negative card cost on its first draw before it can be cleared?
- Can generated cards be drawn and played in time, and do they separate energy producers from their consumers?
- Is the whole deck weak, or is one function missing? Would a broad replacement destroy the only block, damage, or fuel before repairs arrive?
- Which card blocks a critical draw at the worst time, and which weak card can existing effects already handle? What job does an early transition card still do?

Removal usually starts with basic Strikes and Defends. Exhausting them in battle is a gain, not a cost: they stop entering later shuffles, so stronger cards cycle more often. Count a loss only when their output or a specific synergy is still needed.
