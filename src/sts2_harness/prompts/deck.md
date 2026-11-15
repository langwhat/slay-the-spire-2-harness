# Deck

Judge every deck change from the cards' rules as shown, what they do together in a turn, and the fights ahead, in numbers rather than printed values or habit. Supplied knowledge, when present, adds exact rules for owned and offered objects, the HP, powers, and intents of the act's Elites and Boss and of the final act's Bosses, and cores known for this character; a core it lists is a candidate with conditions, not an order. Candidate `links`, when present, list event connections to owned objects as a cross-check; read connections from the card text yourself as well. Card offers are separate decisions: decide each after the earlier ones have changed the deck.

## What the deck must answer

Bosses decide the run. Later acts' Bosses are stronger, and the final act may require two back to back with no recovery between. A deck that wins them meets four demands:

- Early damage: enough to end first-act fights and Elites before their damage piles up.
- Damage to several enemies: single-target damage lowers the sum of their attacks one enemy at a time.
- Block that keeps up with the largest hits, or prevention that scales with them, such as weakening the enemy.
- Growth: damage per turn that keeps rising, so a high-HP Boss dies before its damage outgrows the deck's block. Damage per turn is the damage per energy of the cards played, times the energy, within the cards a turn can play; raising any of the three raises it.

The fights help identify which demand is short. A late kill turn can mean the main cards arrive late because the deck is large or draws little, or they do not grow fast enough once in play. Hands that hold too little block for the hits coming can indicate a shortage of block. `fights` records turns and entering and leaving HP, including healing at battle end; it does not show hands or damage dealt. A fight that took far more turns or HP than comparable fights is evidence of a gap, not proof of its cause.

When supplied knowledge describes the act's Elites and Boss or the final act's Bosses, play those fights out with the current deck, relics, and potions: which cards, on which turns, bring the enemy's HP to zero, and how much of its damage the same hands block until then. Without it, use comparable fights in `fights` to estimate the demands of a longer, harder Boss fight. When history is empty or incomparable, state what is unknown and judge from the visible card rules; do not invent exact enemy HP, damage, or kill turns. Either way the final act's Bosses cannot be outlasted: they need damage speed first, and block enough to survive until it lands.

## What wins fights

- Core: a small linked group with two halves. The source is something that rises during the fight (a stat that stacks, a card that grows each time it is played, a count of events), or a cycle in which draw, energy return, and in-fight exhaust bring the same few cards back every turn. The payoff is the cards that turn the source into damage, so that damage per turn keeps rising or the fight ends within a turn or two of assembly. Later acts are won by the core, because only growth keeps up with their Bosses.
- Support: energy, draw, and block. Block, and anything else that prevents damage, buys the turns in which the core arrives and grows. Energy and draw put more of the core into one turn.
- Transition: a card whose own numbers are strong and that needs no other card. It raises a starting deck at once. Once a core works, each draw of it is a draw that was not the core or its support.

To find links, read each card for what it produces (a stat, block, a card exhausted, discarded, drawn, or generated, HP lost, energy, a status card) and what it grows from, listens for, consumes, or repeats; a link exists when one owned or offered object produces something specific that another consumes. A bonus that every attack or every block card receives is not a link to each of them.

A group with one half is not a core. A debuff, draw, cycling, or energy with nothing owned that grows from it only multiplies the plain cards the deck already plays. The test is the group's damage on the turn it is assembled and two turns later: when the later figure is not clearly higher and the first does not end the fight, the group does not grow.

## What a card costs

Every added card is drawn in place of the cards already owned for the rest of the run. One more circulating card makes each owned card arrive N/(N+1) as often, where N is the circulating deck; `deck_stats` gives the turns one pass through it takes (`baseline_cycle_turns`), the energy an average hand needs (`baseline_hand_cost`), and how many of its playable cards energy cannot pay for (`baseline_unplayed_per_hand`). `added` and `skipped` count this act's completed card reward choices.

- While hands already hold more than energy pays for, a card that costs energy is played only instead of another card; its value is the difference between the two.
- Cards that work only when played in the same turn share a hand far less often than each is drawn, unless something holds or fetches them. A power, or a card that exhausts when played, costs its draw and energy once per fight.
- A draw card's net gain is the cards it draws minus itself; the cards drawn still need energy. An effect that pays per card or per attack is worth the number the turn's energy pays for, and one that needs a counter to reach a threshold is worth nothing unless owned sources reach it within a fight.
- A power returns nothing on the turn it is played. Each further power the deck must play before its damage arrives lengthens the turns spent being hit; a deck that relies on several needs block, relics, or potions to cover them.
- Cards that enemies add during a fight take a larger share of a smaller deck, so a deck that works only while thin is fragile against them.
- Removing a weak card, or upgrading an owned one, improves every later draw and adds no card.

## The decision

1. Name what the deck will kill the next Boss with now, and the demand most short in the fights left.
2. For each candidate, name the demand it answers and what it would be played instead of: the best owned card for the same job, or, while basic cards fill the deck, a basic card. Prioritize the most urgent gap, but a card also earns a place when it solves another clear shortfall in an approaching fight, or completes or multiplies the core. Before a core exists, a card must clearly outplay a basic card at an unmet demand. A card that answers no unmet demand and does not improve the core only dilutes.
3. Skipping is a full choice, compared like any card. A link is a reason to look, not a reason to take: a small bonus, or a pair seldom drawn together, changes nothing. When any number of cards may be chosen, each must pass on its own, and choosing none is valid.

The act changes what a candidate is compared with:

- First act: most of the deck is basic cards, so a strong card is played instead of a basic one, and the deck must first beat this act's Elites and Boss. Take what works at once: early damage, damage to several enemies, block the fights show short, a card that an owned card triggers and that grows, or a source or payoff strong enough to carry a deck before its partners are owned. Two or three such starts can be owned at once. Prefer cards that work the turn they are drawn over cards that pay off only after setup, and do not give up present power for a plan that needs cards not yet owned. A further card for a demand the deck already meets is skipped unless it makes the coming Elites clearly easier.
- Later acts: draws now hold chosen cards, and each offer competes with those. Take the missing half of the core, a card that multiplies it, or a card for the demand the fights show short that outdoes what the deck has for it. A transition card taken now is drawn in place of the core for the rest of the run.
- Final act: few fights are left in which a new card is drawn. Take a card only when it changes how the remaining Boss fights go, ahead of the cards the deck already owns; otherwise skip; removal and upgrades often do more.

The core is the current best answer, not a commitment. When an offered card is itself a source or a payoff, work out the group it would form with owned cards and at most one card the rewards ahead can plausibly supply, and compare that group's damage with the core's against the Bosses ahead. Take it when clearly higher; when its group becomes the stronger one, the old group's cards are transition cards. A card that is neither a source nor a payoff is never a reason to change core.
