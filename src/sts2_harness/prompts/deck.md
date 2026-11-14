# Deck

Supplied knowledge is the record of how this game's cards, relics, and enemies behave: the exact rules of owned and offered objects, the HP, powers, and intents of every Elite this act can produce and of its Boss, the final act's Bosses, and cores known for this character. A candidate's `links` name the owned cards and relics it triggers or is triggered by. Judge every deck change from these rules and from what the cards do together in a turn, in numbers, not from printed values or habit. Card offers are separate decisions: decide each after the earlier ones have changed the deck.

## What wins fights

- Core: a small linked group with two halves. The source is something that rises during the fight (a stat that stacks, a card that grows each time it is played, a count of events), or a cycle in which draw, energy return, and in-fight exhaust bring the same few cards back every turn. The payoff is the cards that turn the source into damage, so that damage per turn keeps rising or the fight ends within a turn or two of assembly. Later acts are won by the core, because only growth keeps up with their enemies.
- Support: energy, draw, and block. Block, and anything else that prevents damage, buys the turns in which the core arrives and grows. Energy and draw put more of the core into one turn.
- Transition: a card whose own numbers are strong and that needs no other card. It raises a starting deck at once. Once a core works, each draw of it is a draw that was not the core or its support.

A link exists when one object produces something specific that another consumes, listens for, or repeats: an event (a card exhausted, discarded, drawn, or played, HP lost, block gained), a generated card, a card returned or replayed, or energy and draws that a named card needs. A bonus that every attack or every block card receives is not a link to each of them.

A group with one half is not a core. A debuff, draw, cycling, or energy with nothing owned that grows from it only multiplies the plain cards the deck already plays. The test is the group's damage on the turn it is assembled and two turns later: when the later figure is not clearly higher and the first does not end the fight, the group does not grow. A core listed in supplied knowledge is a candidate with conditions, not an order, and a group that is not listed can still be real.

## What a card costs

A deck wins by playing its best few cards often, and every card added stands between the deck and those cards in each hand where it is not the best play. Let N be the cards that keep circulating in a fight, D the draw per turn (`base_draw`), and E the energy per turn (`base_energy`). In `deck_stats`, `baseline_cycle_turns` is N/D, `baseline_hand_cost` is the energy an average hand needs to play its playable cards (X costs E), and `baseline_unplayed_per_hand` is how many of them E cannot pay for, cheaper cards first. `added` and `skipped` count this act's completed card reward choices.

- A given card has been drawn by turn t with a chance of about min(1, t*D/N). That is enough for a card that stays in play once played, such as a power. Cards that work only when played in the same turn must also share a hand: two of them do so in one pass through the deck with a chance of about (D-1)/(N-1), and three far less often, unless something holds or fetches them.
- One more circulating card makes every owned card arrive N/(N+1) as often, and the worst card is still drawn. A core that needs several cards together, or a card that grows with each play, needs the smallest deck and the most draw.
- Energy is the limit on a turn: the hand's cards beyond what E pays for are discarded unplayed. While `baseline_unplayed_per_hand` is above zero, an added card that costs energy is played only instead of another card, and its value is the difference between the two.
- A draw card takes up a draw itself: its net gain is the cards it draws minus one. Draw that costs energy also gives up one played card, and the cards drawn still need energy; in hands that E already cannot pay for it adds choice, not plays, except for the zero-cost cards it finds.
- An effect that pays per attack or per card played is worth the number the turn's energy pays for. An effect that needs a counter to reach a threshold is worth nothing unless owned sources reach it within a fight.
- Prevention that scales with the enemy's damage, such as halving it or weakening the enemy, is worth that share of the listed intents on the turns it covers; against large late hits this exceeds any fixed block number.
- A power, or a card that exhausts when played, costs one draw and its energy once per fight rather than every cycle.
- Removing a below-average card raises the frequency of every other card. Upgrading an owned card improves every draw of it and adds no card. Both rank above adding a card that is merely good.

## What the deck is short of

What the deck lacks is measured against the enemies ahead, not against its own card list. For each Elite this act lists and for its Boss, play the fight out against the sequence of intents with the current deck, relics, and potions: which cards, on which turns, bring its HP to zero, and how much of its damage the same hands block until then. Count only what works under the enemy's rules. Against several enemies the attacks add up each turn and single-target damage lowers that sum one enemy at a time.

HP lost in a fight is the enemy's damage per turn less block, times the turns the fight lasts. A late kill turn is a shortage of damage or of speed: the core arrives late because the deck is large or draws little, or it does not grow fast enough once in play. Hands that hold too little block for the hits coming are a shortage of block. Battle reports measure the same things, and a fight that cost a fifth of max HP or more shows a demand the deck does not meet; name its cause. The HP lost per fight, times the fights `ahead` counts, has to fit within current HP plus the rests on the route and still leave the Boss its share.

The run is decided by the final act's Bosses. When supplied knowledge describes them, measure the deck against their HP and damage from the first act: the turn the core is in play, the damage per turn after that, and the HP lost before it.

## The decision

Every candidate is compared with two things: the deck without it, and the owned card it competes with. Skipping is a full choice, compared like any card.

1. Name the one thing that limits the deck in the fights left: the turn the core is in play, its damage once it is, the block in the hands before that, or damage to several enemies.
2. Name the candidate's job and the best owned card that does the same job. In a hand that holds both, energy usually pays for one. The candidate earns a place when it would be the one played, because it does the job clearly better, or when no owned card does its job and that job is the limit just named. A card that does an owned card's job no better is a worse copy: skip it, and upgrade the owned card when a rest site allows.
3. Work out what a normal turn does with the candidate and without it, in damage or block. A link is a reason to look, not a reason to take: a small bonus, or a pair that is seldom drawn together, changes nothing. Against that gain stands one more card between the deck and its core for the rest of the run.

The act changes what a candidate is compared with:

- First act: most of the deck is basic cards, so a strong card is played instead of a basic one, and the deck must first survive this act's Elites and Boss. Take what works at once: a transition card for what the deck cannot yet do, a card that an owned card triggers and that grows, or a source or payoff strong enough to carry a deck before its partners are owned. Two or three such starts can be owned at once. A further card for a demand already met is skipped.
- After the first act: the deck's draws now hold chosen cards, and each offer competes with those. Take the missing half of the core, a card that multiplies it, or a support card that outdoes the best owned card of its job when the fights show that job short. A transition card taken now is drawn in place of the core for the rest of the run.
- Final act: few fights are left in which a new card is drawn, and every added card makes the core later in the Boss fights that decide the run. Take a card only when, in those fights, it would be played ahead of cards the deck already owns. Most offers are skipped; removal and upgrades do more.

The core is the current best answer, not a commitment. When an offered card is itself a source or a payoff, work out the group it would form with owned cards and at most one card that the rewards still ahead can plausibly supply, and compare that group's damage with the core's. Take it when it is clearly higher against the Bosses ahead; when its group becomes the stronger one, the old group's cards are transition cards. A card that is neither a source nor a payoff is never a reason to change core.
