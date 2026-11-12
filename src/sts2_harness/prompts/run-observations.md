# Map

`map_topology` is the act's connections and known Bosses. `dynamic_map` is the current position, the destinations the game currently allows, and the remaining plan. `unknown_odds` is the published odds for unknown rooms. Map node IDs are stable within the act.

A connection or old ink does not make travel legal now; a pending choice can block it. run_plan replaces the remaining plan and never moves. run_next enters the plan's next node. After any movement, read the actual position, and replace a plan whose next edge is missing, ambiguous, or illegal. Automatic travel follows the game's legal destinations, not the plan.

# Run basics

- HP, max HP, gold, deck, relics, and potions carry across rooms. Read them from the current state after every room.
- Battle piles are not the deck: discarding or exhausting in battle removes nothing permanently. Adding, removing, transforming, and upgrading deck cards is permanent.
- Rewards, shops, rest sites, and events offer exactly the operations shown. Decline only where declining is offered. Prices and services are current facts, and a purchase can change later offers.
- Potion slots are limited; a full inventory may require discarding one first.
- Unrevealed rooms and outcomes are unknown until shown.

# Run length

`progress` gives the current act, the number of acts, and the floor; supplied knowledge lists the ascension rules in force. The run is won after every required Boss fight in the last act is won, including its second Boss when the ascension rules require one; nothing has value after the final fight. Later acts' maps are not shown, so their node counts are unknown; supplied knowledge explains card reward rarity and upgrade rolls. A deck change is worth the fights left in which it is drawn, and a plan that still needs cards is worth starting only while enough rewards remain to find them.

# Deck

Supplied knowledge is the record of how this game's cards, relics, and enemies actually behave: the exact rules of owned and offered objects, the HP, powers, and intents of every Elite this act can produce and of its Boss, and whatever it holds about cores known for this character and about the final act's Bosses. A candidate's `links` name the owned cards and relics it triggers or is triggered by. Decide every deck change from these and from the arithmetic below, not from printed numbers or general habits.

Before a deck change, state for each candidate what its rule and its documents say, and what each link does with the linked owned object: what triggers what, how often in a turn, and what grows. Then choose. Card offers are separate decisions: decide each after the earlier ones have changed the deck.

## Kinds of cards

- Core: a small linked group with two halves. The source is something that rises during the fight (a stat that stacks, a card that grows each time it is played, a count of events), or a cycle in which draw, energy return, and in-fight exhaust bring the same few cards back every turn or several times in one turn. The payoff is the cards that turn the source into damage, so that damage per turn keeps rising or the fight ends within a turn or two of assembly. Later acts are won by the core, because only growth keeps up with their enemies.
- Support: energy, draw, and block. Block, and anything else that prevents damage, buys turns: each turn survived is another hand in which the core can arrive and another step of growth. Energy and draw put more cards into one turn, so the core is found sooner and played together; an energy card that works once per fight still pays for that turn.
- Transition: a card whose own numbers are strong and that needs no other card. Added to a starting deck it raises the deck at once. Once a core works, each draw of it is a draw that was not the core or its support: it slows the cycle and lowers how often the core is played.

A link exists when one object produces something specific that another consumes, listens for, or repeats: an event (a card exhausted, discarded, drawn, or played, HP lost, block gained), a generated card, a card returned or replayed, or energy and draws that a named card needs. A bonus that every attack or every block card receives, such as more damage per hit or a debuff on the enemy, is not a link to each of them.

A group with one half is not a core. Applying or extending a debuff, drawing, cycling, or gaining energy, with nothing owned that grows from it, only multiplies the plain cards the deck already plays: it is support for whatever deals the damage. The test is the group's damage on the turn it is assembled and two turns later. When the later figure is not clearly higher and the first does not end the fight, the group does not grow. Many plain attacks are a core only if an average opening hand and its energy kill, or nearly kill, the fights that threaten the run before the enemy's damage matters. A core listed in supplied knowledge is a candidate with conditions, not an order: it counts only when its source and its payoff are both owned or within reach, and a group that is not listed can still be real.

## Draws and energy

A card is worth how often it is drawn and played, not its printed numbers. Let N be the cards that keep circulating in a fight, D the draw per turn (`base_draw`), and E the energy per turn (`base_energy`). In `deck_stats`, `baseline_cycle_turns` is N/D, `baseline_hand_cost` is the energy an average hand needs to play its playable cards (X costs E), and `baseline_unplayed_per_hand` is how many of them E cannot pay for, cheaper cards first. `added` and `skipped` count this act's completed card reward choices.

- A given card is in hand by turn t with a chance of about min(1, t*D/N). A combination of k cards is assembled by turn t with about that chance raised to the power k.
- Adding one circulating card makes every owned card arrive N/(N+1) as often, and a k-card combination assemble (N/(N+1))^k as often. With N=25 and k=3 that is 11% less often per added card and 42% less often after five. A core that needs several cards together, or a card that grows with each play, is worth only the number of times per fight it is drawn and played: it needs the smallest deck and the most draw.
- An added card takes the place of an average draw, not of the deck's worst card, which stays and is still drawn. It raises the deck only if its value when drawn exceeds that average, counting what it gives other owned cards through their rules and only the draws on which it is played. While basic cards are most of the deck, a card clearly stronger per energy does. The average rises as the core forms, so a card worth taking early can be a loss later.
- Energy is the limit on a turn. A turn plays only the cards that E, plus the energy owned cards and relics give that turn, pays for; the rest of the hand is discarded unplayed. Before valuing an addition, state what a normal turn of the current deck plays for that energy. While `baseline_unplayed_per_hand` is above zero, an added card is played only instead of another, and its value is the difference between the two.
- A draw card takes up a draw itself: its net gain is the cards it draws minus one, so a card that draws one only replaces itself. Draw that costs energy also gives up one played card, and the cards drawn still need energy. While hands already hold more than E pays for, draw adds choice, not plays; energy, and draw at zero cost, add plays.
- A card that pays out for each attack or each card played in a turn is worth the number of those the turn's energy pays for, not the number in the deck.
- Prevention that scales with the enemy's damage, such as halving it or weakening the enemy, is worth that share of the listed intents on the turns it covers. Against large late hits this exceeds any fixed block number. Count how often owned cards meet its condition.
- An effect that pays only when a counter reaches a threshold needs enough owned sources of that counter to reach it within a fight. One or two such cards are worth only their other printed numbers.
- A power, or a card that exhausts when played, costs one draw and its energy once per fight rather than every cycle.
- Removing, transforming, or exhausting a below-average card raises the frequency of every other card. Basic cards, and transition cards once a core works, are the first to go, and removing them ranks above adding cards.
- Before adding a second card for a job an owned card already does, compare it with upgrading the owned one. The upgrade improves every draw of that card and adds no card; the copy improves one draw in N+1 and makes every other card arrive less often. A deck needs a few good support cards, upgraded, not many plain ones.
- Assess an offered card at its base version unless an upgrade opportunity is actually reserved for it; several cards cannot claim the same opportunity.

## The fights ahead

What the deck is short of is measured against the enemies ahead, not against its own card list. The next Elite can be any of those listed. For each listed Elite and the Boss, play the fight out against its sequence of intents with the current deck, relics, and potions, and state:

- what wins it: which cards, on which turns, bring its HP to zero, and the kill turn that gives. Use the cards, energy, `baseline_cycle_turns`, and any growth; if nothing owned wins it, say so.
- the HP lost up to the kill turn: its damage on each turn less the block the same hands can play.
- the HP lost on turn 1 in the worst case: its first intent against the weakest plausible opening hand.

An enemy's rule can void a kind of card, such as block gained on every first hit against small attacks, or a countdown that ends the fight; count only damage and block that work under it. Against several enemies the attacks add up each turn and single-target damage lowers that sum one enemy at a time, so damage to all enemies and block decide those fights. A core works when these results show its cards normally in play before the enemy's damage, growth, or countdown decides the fight.

HP lost in a fight is the enemy's damage per turn less block, times the turns the fight lasts. A late kill turn is a shortage of damage, and more block only makes each of those turns cheaper; hands that hold too little block for the hits coming are a shortage of block. Battle reports measure the same things: a fight that cost a fifth of max HP or more shows a demand the deck does not meet. Name its cause (several attackers, one large hit, a late kill turn, cards added to the deck) and count the deck as short of the answer until it has one. The HP lost per fight, times the fights `ahead` counts, has to fit within current HP plus the rests on the route and still leave the Boss its share; what does not fit is paid for with rests, and every rest is an upgrade not made.

The run is decided by the final act's Bosses. When supplied knowledge describes them, measure the core's growth against them from the first act, and among cards that serve the present act equally take the one that also answers them. A card whose main use lies in a later act is worth its draws now only when current HP and the fights before the next rest can carry it.

## Choosing

Being better than the average draw is necessary, not sufficient: the card must also supply what the fights ahead show the deck to be short of. A few points of printed value that no coming fight needs are not a reason.

Before a core works, the deck needs all three kinds, because the starting deck beats neither the first act's Elites nor its Boss:

- Take a card that an owned card triggers, or that triggers an owned card, and that grows, ahead of a card with larger printed numbers: it works now and can become the core.
- A source or a payoff strong enough to carry a deck is worth taking before its partners are owned, while `ahead` and the acts left hold enough rewards to find them. Two or three such starts can be owned at once; the one that comes together becomes the core.
- Take transition cards for what the deck cannot yet do against this act's Elites and Boss: damage while the kill turn is too late, damage to all enemies when a fight has several, block while the HP lost is too high. A further card for a demand already met is skipped.

Once a core works, the fights ahead show which of two things is short:

- Damage is short when what grows cannot overtake the enemy however long the fight lasts. Add a source or a card that multiplies it: something that triggers the core, is triggered by it, or turns its growth into damage.
- Support is short when the core would win given more turns or an earlier start. Add block to buy the turns, and draw and energy to bring the core together sooner; another attack makes it worse. Value an offered energy, draw, or block card by what it buys for the core, turns survived against the listed intents or extra plays in a turn, after its costs and conditions, not by comparing its printed numbers with an attack's.
- Support is enough when the core is normally in play before the listed enemies decide the fight, the hands before that block most of what is coming, and no fight costs a fifth of max HP. Past that point more of it only delays the core.
- Take no more transition cards; a card whose only connection is to an owned card outside the core is one.
- When neither is short, nothing needs adding: skip, remove, or upgrade.

The core is the current best answer, not a commitment. Read the whole deck again at every deck change; a deck can own parts of several groups, and the first one found is seldom the best. When an offered card is itself a source or a payoff, work out the group it would form with owned cards and at most one more card that the rewards still ahead can plausibly supply, and compare that group's damage, on its assembly turn and two turns later, with the core's. Take it when it is clearly higher against the Bosses ahead, even though the card has no link to the core. When its group becomes the stronger one, that group is the core and the old group's cards are transition cards. Such cards are rare: most offers hold none. A card that is neither a source nor a payoff is never a reason to change core, and a change that needs three or more cards not yet owned is not worth starting.

# Rooms and route

- Shop: compare the affordable combinations of cards, relics, removal, and potions with keeping the gold for a reachable later use. A bought card is judged like any added card, and removing a basic card, or a transition card once a core works, is often the best purchase. Buy a potion for a specific fight it changes.
- Rest site: upgrade when HP covers the fights before the next recovery under plausible play; rest when it does not, or when the recovered HP opens a better route. Upgrade the card whose upgrade most changes the fights ahead; where owned cards grow together, that is usually the one that raises their growth. Uncertain draws alone are not a reason to rest. When resting keeps displacing upgrades, the deck loses too much HP per fight; fix what the fights show to be short at the next deck change.
- Route: plan through the next certain recovery, counting the fights and possible unknown-room battles before it. Take the more dangerous branch when its rewards outweigh the HP it is expected to cost and a fallback remains. Elites and Bosses are the fights the deck must be built for. Enter an Elite only when the deck has an answer to every Elite the act can produce and HP to spare for its worst opening; otherwise route around it and strengthen the deck first.
- Alternative rewards, such as a choice of relics: HP restored between fights arrives after the damage is taken and does not shorten a fight or lower the damage inside it. Prefer what acts inside fights (energy, draw, damage, block, prevention), which lowers the HP lost in every fight left, unless current HP cannot survive the next fight without the healing.
- Cards that an event or relic lets the deck add are judged like any added card, one at a time. When any number may be chosen, each chosen card must pass on its own, and none is a valid choice.
- Events: charge a burden such as a curse, lost HP, or a lost card at its cost until a reachable remedy, and check that the next unavoidable fight stays survivable if a gamble fails.

After a battle report, change only what the evidence changes. One strong or weak fight is one observation, and a cause the report does not show is a hypothesis.
