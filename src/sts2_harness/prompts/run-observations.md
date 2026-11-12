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

`progress` gives the current act, the number of acts, and the floor; supplied knowledge lists the ascension rules in force. The run is won when the last act's Boss fight is won, and nothing has value after it. `ahead` counts what the remaining plan holds before this act's Boss; later acts' maps are not shown, so expect about as many of each node kind in every remaining act. `card_odds` gives the per-card rarity and upgrade chances of the next reward of each fight kind. A deck change is worth the fights left in which it is drawn, and a plan that still needs cards is worth starting only while enough rewards remain to find them.

# Deck

Supplied knowledge is the record of how this game's cards, relics, and enemies actually behave: the exact rules of owned and offered objects, and the HP, powers, and intents of every Elite this act can produce and of its Boss. A candidate's `links` name the owned cards and relics it triggers or is triggered by. Decide every deck change from these, not from printed numbers or general habits.

Before a deck change, state for each candidate what its rule and its documents say, and what each link does with the linked owned object: what triggers what, how often in a turn, and what grows. Then choose.

- Act 1: the starting deck beats neither the act's Elites nor its Boss. Take a card whenever one is clearly stronger than a basic starting card, choosing by what the listed Elites and Boss demand that the deck cannot yet do. That a card does not grow, or belongs to no plan, is not a reason to skip it in this act.
- Later acts: fights are long and enemies large, so damage has to come from owned cards and relics that feed one another and grow during the fight. Energy, draw, and block keep the deck alive and bring those cards together. Take what joins or serves that group and skip the rest. Read the whole deck again each time; the strongest linked group is the one to build, whether or not it was the earlier plan.
- Every added card makes every other card arrive later, and every removed card makes the rest arrive sooner.

# Rooms and route

- Shop: compare the affordable combinations of cards, relics, removal, and potions with keeping the gold for a reachable later use. A bought card is judged like any added card, and removing a weak card is often the best purchase. Buy a potion for a specific fight it changes.
- Rest site: upgrade when HP covers the fights before the next recovery under plausible play; rest when it does not, or when the recovered HP opens a better route. Upgrade the card whose upgrade most changes the fights ahead; where owned cards grow together, that is usually the one that raises their growth. Uncertain draws alone are not a reason to rest. When resting keeps displacing upgrades, the deck is short of block or draw; fix that at the next deck change.
- Route: plan through the next certain recovery, counting the fights and possible unknown-room battles before it. Take the more dangerous branch when its rewards outweigh the HP it is expected to cost and a fallback remains. Elites and Bosses are the fights the deck must be built for. Enter an Elite only when the deck has an answer to every Elite the act can produce and HP to spare for its worst opening; otherwise route around it and strengthen the deck first.
- Events: charge a burden such as a curse, lost HP, or a lost card at its cost until a reachable remedy, and check that the next unavoidable fight stays survivable if a gamble fails.

After a battle report, change only what the evidence changes. One strong or weak fight is one observation, and a cause the report does not show is a hypothesis.
