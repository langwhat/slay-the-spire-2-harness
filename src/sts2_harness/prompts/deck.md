# Deck

A deck change is judged by what the deck can do in the fights ahead, worked out from the cards' rules as shown and how they play together in a turn, in numbers rather than printed values or habit. Card offers are separate decisions: decide each after the earlier ones have changed the deck.

Supplied knowledge, when present, adds exact rules for owned and offered objects, the HP, powers, and intents of the act's Elites and Boss and of the final act's Bosses, and cores known for this character. A core it lists is a candidate with conditions, not an order.

## What the deck must answer

Bosses decide the run. Later acts' Bosses are stronger, and the final act may require two back to back with no recovery between. A deck that wins them meets four demands:

- Early damage: enough to end first-act fights and Elites before their damage piles up.
- Damage to several enemies: single-target damage lowers the sum of their attacks one enemy at a time.
- Block that keeps up with the largest hits, or prevention that scales with them, such as weakening the enemy. Name the turn it is missing on: the opening, the turns spent setting up, or a later large hit. More block cards do not help a deck that has block but not on that turn; carried block, draw, or defense from relics and powers may.
- Growth: damage per turn that keeps rising, so a high-HP Boss dies before its damage outgrows the deck's block. Damage per turn is the damage per energy of the cards played, times the energy, within the number of cards a turn can play; raising any of the three raises it.

The final act's Bosses cannot be outlasted: they need damage speed first, and block enough to survive until it lands.

Relics count toward the demands. A relic that blocks, prevents damage, or gives energy or draw does that job without a draw or energy, so the cards need less of it; a deck without such relics needs its cards to carry it.

## Reading the fights

`fights` lists this run's most recent battles, oldest first: floor, room kind, turns, and HP on entering and on leaving, which includes healing granted when the battle ends. It does not show hands or damage dealt. A fight that took far more turns or HP than comparable fights is evidence of a gap, not proof of its cause:

- A late kill turn: the main cards arrive late because the deck is large or draws little, or they do not grow fast enough once in play.
- HP lost although the fight ended on time: block is short on the turns the enemy hits hardest.

`use.observed_plays` counts each owned card definition's plays in the sampled recent battles. Chosen cards with few plays while basic cards are still played show hands that hold more than they can play; another card of that job joins the unplayed.

When supplied knowledge describes the act's Elites and Boss or the final act's Bosses, play those fights out with the current deck, relics, and potions: which cards, on which turns, bring the enemy's HP to zero, and how much of its damage the same hands block until then. Without it, use comparable fights in `fights` to estimate a longer, harder Boss fight. When history is empty or incomparable, say what is unknown and judge from the visible card rules; do not invent enemy HP, damage, or kill turns.

## What a deck is made of

- Core: a small linked group with two halves. The source is something that rises during the fight (a stat that stacks, a card that grows each time it is played, a count of events), or a cycle in which draw, energy return, and in-fight exhaust bring the same few cards back every turn. The payoff turns the source into damage, so that damage per turn keeps rising or the fight ends within a turn or two of assembly. Later acts are won by the core, because only growth keeps up with their Bosses.
- Support: energy, draw, and block. Block, and anything else that prevents damage, buys the turns in which the core arrives and grows. Energy and draw put more of the core into one turn.
- Transition: a card whose own numbers are strong and that needs no other card and no play order. It raises a starting deck at once and is the main source of first-act power. Once a core works, each draw of it is a draw that was not the core or its support.

To find links, read each card for what it produces (a stat, block, a card exhausted, discarded, drawn, or generated, HP lost, energy, a status card) and what it grows from, listens for, consumes, or repeats. A link exists when one owned or offered object produces something specific that another consumes. A bonus that every attack or every block card receives is not a link to each of them, and it pays according to how the deck hits: a bonus per hit pays most on many small hits, a multiplier on large ones.

A group with one half is not a core. A debuff, draw, cycling, or energy with nothing owned that grows from it only multiplies the plain cards the deck already plays. Test a group by its damage on the turn it is assembled and two turns later: when the later figure is not clearly higher and the first does not end the fight, the group does not grow.

The core is the current best answer, not a commitment. When an offered card is itself a source or a payoff, work out the group it would form with owned cards and at most one card the rewards ahead can plausibly supply, and compare that group's damage with the core's against the Bosses ahead. Take it when clearly higher; once its group is the stronger one, the old group's cards are transition cards. A card that is neither a source nor a payoff is never a reason to change core.

## What a turn plays

A turn draws `base_draw` cards and plays only what its energy pays for; the rest are discarded unplayed. A deck is as strong as what its usual hand plays, and a card matters only on the turns it is played.

- An added card takes its draw from whichever card would have come instead, the deck's best cards as often as its basic ones. With N circulating cards (`deck_stats.circulating`), each card is in hand once every N/`base_draw` turns: drawing five, a card in a 15-card deck comes twice in a six-turn fight, in a 30-card deck once.
- In hand, a card that costs energy adds no play. It is played instead of the weakest card that hand would have paid for and is worth the difference; when it is no better, it is discarded and its draw bought nothing.
- So what a candidate must beat depends on what the usual hand holds. Count the circulating cards better than a basic card by job (damage, block or prevention, draw, energy); the usual hand holds `base_draw`/N of each count. A different way of doing an owned job is the same job. A turn that also blocks pays for fewer damage cards than its energy alone would: once the usual hand holds as many chosen damage cards as that turn pays for, a new damage card competes with them, not with a basic Strike, while the same hand may still block with basic Defends. Each job fills separately, and damage fills first.
- Energy, 0-cost cards, and draw that nets cards enlarge the turn instead of competing for it. A draw card nets the cards it draws minus itself, and they still need energy: where hands already hold more than energy pays for, draw adds choice and energy adds plays. An effect that pays per card or per attack is worth the number the energy pays for.
- A second copy competes for the same play as the first. It adds only on turns where the hand would play both ahead of its next card, and nothing while the first copy's effect still lasts, as with a debuff or a setup. Upgrades and removals are few, so one strong card for a job, upgraded, does more than several.
- A card is first drawn on turn (N/`base_draw` + 1)/2 on average. A power or setup pays only on the turns after it is played, less the play its energy displaced; like a card that exhausts when played, it costs its draw and energy once per fight. Count any immediate benefit, and the turns spent being hit when several must be played before damage arrives.
- Cards that work only when played in the same turn share a hand far less often than each is drawn, unless something holds or fetches them. A chain that needs to keep drawing cards of one kind stalls when those cards are already in hand or spent.
- A card with a condition is worth its numbers on the turns the condition holds. Count what the deck already supplies toward it in an ordinary fight, including cards that exhaust themselves. Short by one piece of a kind this character's rewards and shops offer routinely, it is the scarce half of a pair: take the scarce half when it appears. Short by several pieces, or a rare one, it is a plan, not yet a card.
- Cards that enemies add during a fight take a larger share of a smaller deck, so a deck that works only while thin is fragile against them. A small deck is not a problem in itself; a small deck without strong numbers is.
- A character with base_orb_slots 0 gains one orb slot when it channels an orb while its current orb capacity is 0; channeling into a full, nonempty queue first evokes its front orb.
- Removing a weak card, or upgrading an owned one, improves every later hand and adds no card.

Nearly every offer holds a card that beats a basic card and has some use ahead; so will every offer left in the run. A ground for taking that is true of all of them builds the deck that takes them all, in which every owned card, the core included, arrives far less often by the final Bosses. Judge the ground by that deck. `added` and `skipped` count this act's completed card reward choices.

## The decision

1. From the deck alone, before weighing any candidate: what a usual hand plays against the next Boss; for damage and for block, the weakest card that hand still pays for; and what limits the turn: energy (cards left unplayed), cards (energy left over), arrival (the core comes late), or the numbers of the cards played.
2. Put each candidate into that hand in place of a card drawn at random, not in place of its worst card.
   - It enlarges the turn, or does a job no owned card does and the fights ahead require (damage to several enemies, block for the largest hit, a source or a payoff): judge it by the turn it is drawn on.
   - It does a job owned cards do: it earns its draw only when that hand plays it ahead of the card found in step 1. A card equal to an owned one is not ahead of it. When it is ahead, the card it beats is the next removal.
   - It starts, completes, or multiplies a core: judge the group as under "What a deck is made of".
3. Skipping keeps every owned card arriving as often as now; later offers, removals, and upgrades remain. When any number of cards may be chosen, place each after the ones already chosen; choosing none is valid.

The reason names the owned card the candidate is played ahead of, or the job no owned card does; a skip names the owned card that already does the candidate's job.

The act changes what the usual hand holds:

- First act: hands are mostly basic cards, so the first strong cards of each job are played at once, and the deck must first beat this act's Elites and Boss. Take early damage, damage to several enemies, block better than a basic Defend for the hits the fights show, draw and energy, and a source or payoff that starts a core; two or three starts can be owned at once. While HP covers the next fights, a scarce half is worth a few weaker turns now. By the Boss the usual hand should no longer pay for a basic Strike; from then on more damage has to beat chosen damage.
- Later acts: hands hold chosen cards, and each offer competes with those. Take the missing half of the core, a card that multiplies it, a card that enlarges the turn, or one that beats the owned card of its job on the turns the fights show short. A transition card taken now is drawn in place of the core for the rest of the run.
- Final act: few fights are left in which a new card is drawn. Take a card only when it changes how the remaining Boss fights go, ahead of the cards the deck already owns; spare gold is not a ground, since a bought card still costs its draw and energy in every hand left. Removal and upgrades often do more.

## Choices that change the piles

When the choice generates, retains, removes, or replaces cards:

- By the critical turn, what is the net change to the piles after exhaustion and continued generation? What does a negative card cost on its first draw before it can be cleared?
- Can generated cards be drawn and played in time, and do they separate energy producers from their consumers?
- Is the whole deck weak, or is one function missing? Would a broad replacement destroy the only block, damage, or fuel before repairs arrive?
- Which card blocks a critical draw at the worst time, and which weak card can existing effects already handle? What job does an early transition card still do?

Removal usually starts with basic Strikes and Defends. Exhausting them in battle is a gain, not a cost: they stop entering later shuffles, so stronger cards cycle more often. Count a loss only when their output or a specific synergy is still needed.
