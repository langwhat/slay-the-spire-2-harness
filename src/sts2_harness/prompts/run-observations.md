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

Judge every optional deck change by how often cards are drawn and played, not by a card's printed numbers alone. Let N be the cards that keep circulating in a fight and D the cards drawn per turn.

- A given card is in hand by turn t with a chance of about min(1, t*D/N). A combination of k cards is assembled by turn t with about that chance raised to the power k.
- Adding one circulating card makes every owned card arrive N/(N+1) as often, and a k-card combination assemble (N/(N+1))^k as often. With N=25 and k=3 that is 11% less often per added card and 42% less often after five.
- The added card raises the deck's output only if its value when drawn exceeds the average value of a card drawn from the current deck. Its value includes the extra output it gives other owned cards through their rules, and counts only the draws on which it is actually played.
- When hands already hold more playable cards than energy, the added card is played only instead of another card. Its value is then the difference between the two, not its printed number.
- Measure value in the fights that threaten the run: output that ends them sooner or prevents HP loss there.
- The average rises as the deck's strongest combination forms, so a card worth taking early can be a loss later.
- A power, or a card that exhausts when played, costs one draw and its energy once per fight rather than every cycle.
- Removing, transforming, or exhausting a below-average card raises the frequency of every other card.
- When N <= D the whole deck is drawn every turn; each added circulating card ends that.
- Assess an offered card at its base version unless an upgrade opportunity is actually reserved for it; several cards cannot claim the same opportunity.

Take an offered card when this comparison favors it over adding nothing, and choose the strongest such card. When no candidate passes, skip where skipping is offered. Card offers are separate decisions: decide each after the earlier ones have changed the deck.

# Rooms and route

- Shop: compare the affordable combinations of cards, relics, removal, and potions with keeping the gold for a reachable later use. A bought card must pass Deck arithmetic like any added card, and removing a below-average card is often the best purchase. Buy a potion for a specific fight it changes.
- Rest site: upgrade when HP covers the fights before the next recovery under plausible play; rest when it does not, or when the recovered HP opens a better route. The best upgrade is the card the strongest turn plays most, or the one that removes its bottleneck. Uncertain draws alone are not a reason to rest.
- Route: plan through the next certain recovery, counting the fights and possible unknown-room battles before it. Take the more dangerous branch when its rewards outweigh the HP it is expected to cost and a fallback remains. Elites and Bosses are the fights the deck must be built for.
- Events: charge a burden such as a curse, lost HP, or a lost card at its cost until a reachable remedy, and check that the next unavoidable fight stays survivable if a gamble fails.

After a battle report, change only what the evidence changes. One strong or weak fight is one observation, and a cause the report does not show is a hypothesis.
