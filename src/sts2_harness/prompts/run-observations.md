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

`progress` gives the current act, the number of acts, and the floor; supplied knowledge lists the ascension rules in force. The run is won after every required Boss fight in the last act is won, including its second Boss when the ascension rules require one; nothing has value after the final fight. `ahead` counts the remaining planned node kinds before this act's Boss when the accepted route is one chain reaching it; otherwise it is omitted. Later acts' maps are not shown, so their node counts are unknown. Supplied knowledge explains card reward rarity and upgrade rolls; `card_rarity_offset` is the current native rarity adjustment, which changes after generated encounter-reward candidates. A deck change is worth the fights left in which it is drawn, and a plan that still needs cards is worth starting only while enough rewards remain to find them.

# Deck arithmetic

Judge every optional deck change by how often cards are drawn and played, not by a card's printed numbers alone. Let N be the cards that keep circulating in a fight and D the native base draw per turn. `deck_stats` reports native `base_draw` and `base_energy`, without conditional relic or combat modifiers. Under those base assumptions, `baseline_cycle_turns` is N/base_draw, `baseline_hand_cost` is the energy an average hand of base_draw cards would need to play its playable cards (X costs base_energy), and `baseline_unplayed_per_hand` counts cards that hand cannot play within base_energy, paying cheaper cards first. These are deck comparisons, not predictions of a modified combat turn. `added` and `skipped` count this act's completed card reward choices.

- A given card is in hand by turn t with a chance of about min(1, t*D/N). A combination of k cards is assembled by turn t with about that chance raised to the power k.
- Adding one circulating card makes every owned card arrive N/(N+1) as often, and a k-card combination assemble (N/(N+1))^k as often. With N=25 and k=3 that is 11% less often per added card and 42% less often after five.
- The added card raises the deck's output only if its value when drawn exceeds the average value of a card drawn from the current deck. Its value includes the extra output it gives other owned cards through their rules, and counts only the draws on which it is actually played.
- When hands already hold more playable cards than energy (`baseline_unplayed_per_hand` above zero), the added card is played only instead of another card. Its value is then the difference between the two, not its printed number.
- An added card does not replace the deck's worst card. The worst card stays and is still drawn; the new card takes the place of an average draw. While basic starting cards are most of the deck, they dominate that average, so a card clearly stronger per energy can raise it. As basic cards become a minority, compare the addition with the stronger current average rather than with a basic card.
- Measure value in the fights that threaten the run: output that ends them sooner or prevents HP loss there.
- The average rises as the deck's strongest combination forms, so a card worth taking early can be a loss later.
- A power, or a card that exhausts when played, costs one draw and its energy once per fight rather than every cycle.
- Removing, transforming, or exhausting a below-average card raises the frequency of every other card.
- When N <= D, the draw budget can cover one full cycle per turn; additions above D break that budget. Recheck draw, energy, retention, and reshuffle rules before claiming a repeatable cycle.
- Assess an offered card at its base version unless an upgrade opportunity is actually reserved for it; several cards cannot claim the same opportunity.

Being better than the average draw is necessary, not sufficient: the card must also supply what Deck shows the deck to be short of against the fights ahead. A few points of printed value that no coming fight needs are not a reason. Card offers are separate decisions: decide each after the earlier ones have changed the deck.

# Deck

Supplied knowledge is the record of how this game's cards, relics, and enemies actually behave: the exact rules of owned and offered objects, and the HP, powers, and intents of every Elite this act can produce and of its Boss. A candidate's `links` name the owned cards and relics it triggers or is triggered by. Decide every deck change from these, not from printed numbers or general habits.

Before a deck change, state for each candidate what its rule and its documents say, and what each link does with the linked owned object: what triggers what, how often in a turn, and what grows. Then choose.

Every card in a deck is one of three kinds:

- Core: a few owned cards and relics that trigger one another and grow during a fight: a stat that stacks and is multiplied by several hits, a card that grows each time it is played, or a payoff for a repeated event. One growth source and the two to four cards that turn it into damage are a complete core. Later acts are won by the core, because only growth keeps up with their enemies.
- Support: energy, draw, and block. Block, and anything else that prevents damage, buys turns: every turn survived is another hand in which the core's cards can arrive and another step of growth. Energy and draw put more cards into one turn, so the core's cards are found sooner and several are played together; an energy card that works once per fight still pays for that turn.
- Transition: a card whose own numbers are strong and that needs no other card. Added to a starting deck it raises the deck at once. Once a core works, every draw of it is a draw that was not the core or its support: it slows the cycle and lowers how often the core is played.

What the deck is short of is measured against the enemies ahead, not against its own card list. The next Elite can be any of those listed. For each listed Elite and the Boss, play the fight out against its sequence of intents with the current deck, relics, and potions, and state:

- what wins it: which cards, on which turns, bring its HP to zero, and the kill turn that gives. Use the cards, energy, `baseline_cycle_turns`, and any growth; if nothing owned wins it, say so.
- the HP lost up to the kill turn: its damage on each turn less the block the same hands can play.
- the HP lost on turn 1 in the worst case: its first intent against the weakest plausible opening hand.

An enemy's rule can void a kind of card, such as block gained on every first hit against small attacks, or a countdown that ends the fight. Count only damage and block that work under it. A core works when these results show its cards normally in play before the enemy's damage, growth, or countdown decides the fight.

Before a core works, the deck needs all three kinds, because the starting deck beats neither Act 1's Elites nor its Boss:

- Take a card that an owned card triggers, or that triggers an owned card, and that grows, ahead of a card with larger printed numbers: it works now and can become the core.
- Take transition cards for what the results show the deck cannot yet do against this act's Elites and Boss: damage while the kill turn is too late, damage to all enemies when an Elite has several, block while the HP lost is too high. A further card for a demand already met is skipped.
- Many plain attacks are a core only if an average opening hand and its energy kill, or nearly kill, the fights that threaten the run before the enemy's damage matters. Otherwise they are transition cards.

Once a core works, the results show which of two things is short:

- Damage is short when what grows cannot overtake the enemy however long the fight lasts. Add a growth source or a card that multiplies it: something that triggers the core, is triggered by it, or turns its growth into damage.
- Support is short when the core would win given more turns or an earlier start. Add block to buy the turns, and draw and energy to bring the core together sooner; another attack makes it worse. Value an offered energy, draw, or block card by what it buys for the core, turns survived against the listed intents or extra plays in a turn, not by comparing its printed numbers with an attack's. Count its draw and energy returns after its costs and conditions.
- Support is enough when the core is normally in play before the listed enemies decide the fight and the hands before that block most of what is coming. Past that point more of it only delays the core, and removal does more.
- Take no more transition cards; a card whose only connection is to an owned card outside the core is one. It adds no growth, is drawn in place of block or draw, and is played in place of a core card. The transition cards already owned are the first cards to remove.
- A core that needs several cards together, or a card that grows with each play, is worth only the number of times per fight it is drawn and played. It needs the smallest deck and the most draw.
- When neither is short for the fights ahead, nothing needs adding: skip, remove, or upgrade.

HP lost because hands held too little block is paid for at rest sites, and every rest is an upgrade not made. Read the whole deck again each time; when another group of owned cards is stronger, that group becomes the core.

# Rooms and route

- Shop: compare the affordable combinations of cards, relics, removal, and potions with keeping the gold for a reachable later use. A bought card is judged like any added card, and removing a basic card, or a transition card once a core works, is often the best purchase. Buy a potion for a specific fight it changes.
- Rest site: upgrade when HP covers the fights before the next recovery under plausible play; rest when it does not, or when the recovered HP opens a better route. Upgrade the card whose upgrade most changes the fights ahead; where owned cards grow together, that is usually the one that raises their growth. Uncertain draws alone are not a reason to rest. When resting keeps displacing upgrades, the deck is short of block or draw; fix that at the next deck change.
- Route: plan through the next certain recovery, counting the fights and possible unknown-room battles before it. Take the more dangerous branch when its rewards outweigh the HP it is expected to cost and a fallback remains. Elites and Bosses are the fights the deck must be built for. Enter an Elite only when the deck has an answer to every Elite the act can produce and HP to spare for its worst opening; otherwise route around it and strengthen the deck first.
- Events: charge a burden such as a curse, lost HP, or a lost card at its cost until a reachable remedy, and check that the next unavoidable fight stays survivable if a gamble fails.

After a battle report, change only what the evidence changes. One strong or weak fight is one observation, and a cause the report does not show is a hypothesis.
