# Map

`map_topology` is the act's connections and known Bosses. `dynamic_map` is the current position, the destinations the game currently allows, and the remaining plan. `unknown_odds` is the published odds for unknown rooms. Map node IDs are stable within the act.

A connection or old ink does not make travel legal now; a pending choice can block it. run_plan replaces the remaining plan and never moves. run_next enters the plan's next node. After any movement, read the actual position, and replace a plan whose next edge is missing, ambiguous, or illegal. Automatic travel follows the game's legal destinations, not the plan.

# Run basics

- HP, max HP, gold, deck, relics, and potions carry across rooms. Read them from the current state after every room.
- Battle piles are not the deck: discarding or exhausting in battle removes nothing permanently. Adding, removing, transforming, and upgrading deck cards is permanent.
- Rewards, shops, rest sites, and events offer exactly the operations shown. Decline only where declining is offered. Prices and services are current facts, and a purchase can change later offers.
- Potion slots are limited; a full inventory may require discarding one first.
- Unrevealed rooms and outcomes are unknown until shown.

# Deck arithmetic

Judge every optional deck change by how often cards are drawn and played, not by a card's printed numbers alone. Let N be the cards that keep circulating in a fight and D the native base draw per turn. `deck_stats` reports native `base_draw` and `base_energy`, without conditional relic or combat modifiers. Under those base assumptions, `baseline_cycle_turns` is N/base_draw, `baseline_hand_cost` is the energy an average hand of base_draw cards would need to play its playable cards (X costs base_energy), and `baseline_unplayed_per_hand` counts cards that hand cannot play within base_energy, paying cheaper cards first. These are deck comparisons, not predictions of a modified combat turn. `added` and `skipped` count this act's completed card reward choices.

- A given card is in hand by turn t with a chance of about min(1, t*D/N). A combination of k cards is assembled by turn t with about that chance raised to the power k.
- Adding one circulating card makes every owned card arrive N/(N+1) as often, and a k-card combination assemble (N/(N+1))^k as often. With N=25 and k=3 that is 11% less often per added card and 42% less often after five.
- The added card raises the deck's output only if its value when drawn exceeds the average value of a card drawn from the current deck. Its value includes the extra output it gives other owned cards through their rules, and counts only the draws on which it is actually played.
- When hands already hold more playable cards than energy (`baseline_unplayed_per_hand` above zero), the added card is played only instead of another card. Its value is then the difference between the two, not its printed number.
- An added card does not replace the deck's worst card. The worst card stays and is still drawn; the new card takes the place of an average draw. While basic starting cards are most of the deck, the average draw is a basic card, and a card clearly stronger per energy than a basic card raises the deck. Once they are a minority, the same card lowers it.
- Measure value in the fights that threaten the run: output that ends them sooner or prevents HP loss there.
- The average rises as the deck's strongest combination forms, so a card worth taking early can be a loss later.
- A power, or a card that exhausts when played, costs one draw and its energy once per fight rather than every cycle.
- Removing, transforming, or exhausting a below-average card raises the frequency of every other card.
- When N <= D, the draw budget can cover one full cycle per turn; additions above D break that budget. Recheck draw, energy, retention, and reshuffle rules before claiming a repeatable cycle.
- Assess an offered card at its base version unless an upgrade opportunity is actually reserved for it; several cards cannot claim the same opportunity.

Being better than the average draw is necessary, not sufficient. Take a card when this comparison favors it and Deck shape shows the deck is short of what it supplies against the fights ahead; otherwise skip where skipping is offered. A few points of printed value that no coming fight needs are not a reason. Card offers are separate decisions: decide each after the earlier ones have changed the deck.

# Deck shape

Every turn the same energy has to deal damage and prevent it. What the deck is short of is measured against the enemies ahead, not against its own card list. Supplied knowledge gives every Elite this act can produce and the act's Boss: HP, starting powers, and the sequence of intents. The next Elite can be any of them. For each of them, play the fight out against that sequence with the current deck, relics, and potions, and state:

- what wins it: which cards, on which turns, bring its HP to zero, and the kill turn that gives. Use the cards, energy, `baseline_cycle_turns`, and any growth; if nothing owned wins it, say so.
- the HP lost up to the kill turn: its damage on each turn less the block the same hands can play.
- the HP lost on turn 1 in the worst case: its first intent against the weakest plausible opening hand.

What these results show the deck to be short of decides every addition:

- Damage is short when the kill turn comes after the enemy's damage or growth has outrun the deck. Add damage or growth before anything else, whatever plan is recorded.
- Block is short when the kill turn is acceptable and the HP lost is more than the route can spare. Add block, or draw that reaches block; another attack makes it worse.
- Consistency is short when the numbers hold only if the plan's cards arrive together. Add draw, energy, in-fight exhaust, and removal.
- When neither number is short for the fights ahead, nothing needs adding: skip, remove, or upgrade.
- An enemy's rule can void a kind of card, such as block gained on every first hit against small attacks. Count only damage and block that work under it.
- Damage should come from a few cards that grow during the fight: a stat that stacks and is multiplied by several hits, a card that grows each time it is played, or a payoff for a repeated event. One growth source and the two to four cards that turn it into damage are a complete damage plan.
- A plan is complete only when these numbers say it kills the act's Boss in time. A growth source without cards that multiply it, or a single card that grows, is not yet a plan.
- Once the plan is complete, another attack adds no damage. It is drawn in place of block or draw and played in place of a plan card. Take one only when it is better than a plan card and will replace it; the replaced card then becomes a card to remove.
- A complete plan gets stronger by reaching its cards sooner and more often: draw, energy, in-fight exhaust, and removal of everything else. It survives through block. Additions after the plan is complete belong to these.
- A card that grows with each play, and any plan that needs several cards together, is worth only the number of times per fight it is drawn and played. Such a plan needs the smallest deck and the most draw.
- Many plain attacks are a plan only if they end fights before the enemy's damage matters: check that an average opening hand and its energy kill, or nearly kill, the fights that threaten the run. Otherwise they are dead weight.
- Before any plan exists, take transitional damage and block only as far as the next Elite's numbers require, including damage to all enemies when it has several. Prefer cards that can later belong to a plan.
- HP lost because hands held too little block is paid for at rest sites, and every rest is an upgrade not made. A deck that keeps resting is short of block or draw rather than damage, unless its fights run long.

# Rooms and route

- Shop: compare the affordable combinations of cards, relics, removal, and potions with keeping the gold for a reachable later use. A bought card must pass Deck arithmetic like any added card, and removing a below-average card is often the best purchase. Buy a potion for a specific fight it changes.
- Rest site: upgrade when HP covers the fights before the next recovery under plausible play; rest when it does not, or when the recovered HP opens a better route. The best upgrade is the card the strongest turn plays most, or the one that removes its bottleneck. Uncertain draws alone are not a reason to rest. When resting keeps displacing upgrades, the deck is short of block or draw; fix that at the next deck change.
- Route: plan through the next certain recovery, counting the fights and possible unknown-room battles before it. Take the more dangerous branch when its rewards outweigh the HP it is expected to cost and a fallback remains. Elites and Bosses are the fights the deck must be built for. Enter an Elite only when, for every Elite the act can produce, Deck shape names what wins it and the HP lost, worst first turn included, leaves HP to spare; otherwise route around it and build first.
- Events: charge a burden such as a curse, lost HP, or a lost card at its cost until a reachable remedy, and check that the next unavoidable fight stays survivable if a gamble fails.

After a battle report, change only what the evidence changes. One strong or weak fight is one observation, and a cause the report does not show is a hypothesis.
