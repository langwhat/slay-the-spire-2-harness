# Deck

A deck change is judged by what the deck can do in the fights ahead, worked out from the cards' rules as shown and how they play together in a turn, in numbers rather than printed values or habit. Card offers are separate decisions: decide each after the earlier ones have changed the deck.

Supplied knowledge, when present, adds exact rules for owned and offered objects, the HP, powers, and intents of the act's Elites and Boss and of the final act's Bosses, and cores known for this character. A core it lists is a candidate with conditions, not an order. Candidate `links`, when present, list event connections to owned objects as a cross-check; read connections from the card text yourself as well.

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

When supplied knowledge describes the act's Elites and Boss or the final act's Bosses, play those fights out with the current deck, relics, and potions: which cards, on which turns, bring the enemy's HP to zero, and how much of its damage the same hands block until then. Without it, use comparable fights in `fights` to estimate a longer, harder Boss fight. When history is empty or incomparable, say what is unknown and judge from the visible card rules; do not invent enemy HP, damage, or kill turns.

## What a deck is made of

- Core: a small linked group with two halves. The source is something that rises during the fight (a stat that stacks, a card that grows each time it is played, a count of events), or a cycle in which draw, energy return, and in-fight exhaust bring the same few cards back every turn. The payoff turns the source into damage, so that damage per turn keeps rising or the fight ends within a turn or two of assembly. Later acts are won by the core, because only growth keeps up with their Bosses.
- Support: energy, draw, and block. Block, and anything else that prevents damage, buys the turns in which the core arrives and grows. Energy and draw put more of the core into one turn.
- Transition: a card whose own numbers are strong and that needs no other card and no play order. It raises a starting deck at once and is the main source of first-act power. Once a core works, each draw of it is a draw that was not the core or its support.

To find links, read each card for what it produces (a stat, block, a card exhausted, discarded, drawn, or generated, HP lost, energy, a status card) and what it grows from, listens for, consumes, or repeats. A link exists when one owned or offered object produces something specific that another consumes. A bonus that every attack or every block card receives is not a link to each of them, and it pays according to how the deck hits: a bonus per hit pays most on many small hits, a multiplier on large ones.

A group with one half is not a core. A debuff, draw, cycling, or energy with nothing owned that grows from it only multiplies the plain cards the deck already plays. Test a group by its damage on the turn it is assembled and two turns later: when the later figure is not clearly higher and the first does not end the fight, the group does not grow.

The core is the current best answer, not a commitment. When an offered card is itself a source or a payoff, work out the group it would form with owned cards and at most one card the rewards ahead can plausibly supply, and compare that group's damage with the core's against the Bosses ahead. Take it when clearly higher; once its group is the stronger one, the old group's cards are transition cards. A card that is neither a source nor a payoff is never a reason to change core.

## What a card costs

Every added card is drawn in place of the cards already owned, for the rest of the run. One more circulating card makes each owned card arrive N/(N+1) as often, where N is the circulating deck (`deck_stats.circulating`). `added` and `skipped` count this act's completed card reward choices.

- While hands already hold more than energy pays for, a card that costs energy is played only instead of another card; its value is the difference between the two.
- Cards that work only when played in the same turn share a hand far less often than each is drawn, unless something holds or fetches them. A chain that needs to keep drawing cards of one kind stalls when those cards are already in hand or spent.
- A draw card's net gain is the cards it draws minus itself, and the cards drawn still need energy. An effect that pays per card or per attack is worth the number the turn's energy pays for; one that needs a counter to reach a threshold is worth nothing unless owned sources reach it within a fight.
- A power, or a card that exhausts when played, costs its draw and energy once per fight. A power may return nothing on the turn it is played; count any immediate benefit from its rule. When several powers must be played before damage arrives, count the setup turns spent being hit and the block, relics, or potions that cover them.
- Cards that enemies add during a fight take a larger share of a smaller deck, so a deck that works only while thin is fragile against them. A small deck is not a problem in itself; a small deck without strong numbers is.
- A character with base_orb_slots 0 gains one orb slot when it channels an orb while its current orb capacity is 0; channeling into a full, nonempty queue first evokes its front orb.
- Removing a weak card, or upgrading an owned one, improves every later draw and adds no card.

## The decision

1. Name what the deck will kill the next Boss with now, and the demand most short in the fights left.
2. For each candidate, name the demand it answers and what it would be drawn and played instead of: the existing providers of that effect and the key cards whose draws it takes, not only a weak basic card. Being stronger than a basic card, cheap, or compatible with one owned bonus is not enough on its own.
3. Take a card that answers the most urgent unmet demand clearly better than the deck already does, that solves another clear shortfall in an approaching fight, or that completes or multiplies the core. A card that answers no unmet demand and does not improve the core only dilutes.
4. Skipping is a full choice, compared like any card. A link is a reason to look, not a reason to take: a small bonus, or a pair seldom drawn together, changes nothing. When any number of cards may be chosen, each must pass on its own, and choosing none is valid.

The reason states what the deck gains over skipping, after the draws the card takes; a skip states why the best candidate's gain is not worth them.

The act changes what a candidate competes with:

- First act: most of the deck is basic cards, so a strong card is played instead of a basic one, and the deck must first beat this act's Elites and Boss. Take what works the turn it is drawn: early damage, damage to several enemies, block the fights show short, a card that an owned card triggers and that grows, or a source or payoff strong enough to carry a deck before its partners are owned. Two or three such starts can be owned at once. Do not give up present power for a plan that needs cards not yet owned. A further card for a demand the deck already meets is skipped unless it makes the coming Elites clearly easier.
- Later acts: draws now hold chosen cards, and each offer competes with those. Take the missing half of the core, a card that multiplies it, or a card for the demand the fights show short that outdoes what the deck has for it. A transition card taken now is drawn in place of the core for the rest of the run.
- Final act: few fights are left in which a new card is drawn. Take a card only when it changes how the remaining Boss fights go, ahead of the cards the deck already owns; otherwise skip. Removal and upgrades often do more.

## Questions for the offer

Ask the questions that fit the offer. They direct attention; they are not a form to fill. Compare a plausible turn with and without each serious candidate against the same fights.

- Is the needed effect missing, or too small, too late, too costly, or unreliable on the dangerous turn?
- Are damage, block, and draw all relying on the same card or combination? What still works when that card arrives late?
- What does the candidate change before the core is in play and after? Does its main function still matter once the core runs?
- Does its output match the size and duration of the actual gap?

When the candidate depends on setup or repeated use:

- Is the problem reaching setup, or lacking output after it? Which of those does this card fix?
- What can it do before the core arrives? Does it make the most dangerous opening turn harder or delay the only core card?
- Which cards must arrive together and be paid for in order, and is the result ready before the enemy's deadline?
- Is its cost paid once or on every use? How many uses beat a one-time alternative, and can draw, energy, and retrieval supply them before the fight is decided?
- Is the bottleneck a resource, or a playable way to spend it? Is more draw needed, or room to hold the needed cards together?

When the candidate adds damage, block, or another copy:

- Which owned damage or block cards receive the proposed bonus, and how often before it matters?
- Does the next threat need faster damage, more targets, or a particular hit pattern? Can the current damage already win it?
- Does the deck still have a way to beat each Boss it can meet?
- Does another copy stack, share a counter, improve access, or only repeat a role that already works? Which later, stronger card would it also dilute?

An existing damage bonus does not justify every compatible attack, and an existing defensive effect does not rule out another source that compounds it.

When the choice generates, retains, removes, or replaces cards:

- By the critical turn, what is the net change to the piles after exhaustion and continued generation? What does a negative card cost on its first draw before it can be cleared?
- Can generated cards be drawn and played in time, and do they separate energy producers from their consumers?
- Is the whole deck weak, or is one function missing? Would a broad replacement destroy the only block, damage, or fuel before repairs arrive?
- Which card blocks a critical draw at the worst time, and which weak card can existing effects already handle? What job does an early transition card still do?

Removal usually starts with basic Strikes and Defends. Exhausting them in battle is a gain, not a cost: they stop entering later shuffles, so stronger cards cycle more often. Count a loss only when their output or a specific synergy is still needed.
