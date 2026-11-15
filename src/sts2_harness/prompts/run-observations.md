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

`progress` gives the current act, the number of acts, and the floor; `run_rules` lists the ascension rules in force. The run is won after every required Boss fight in the last act is won, including its second Boss when the ascension rules require one; nothing has value after the final fight. Later acts' maps are not shown, so their node counts are unknown. A deck change is worth the fights left in which it is drawn, and a plan that still needs cards is worth starting only while enough rewards remain to find them.

# How a run is won

A run is a race between the deck's power and the Boss fights at the end of each act, which cannot be avoided and grow harder act by act. Power comes from relics, cards, upgrades, removals, and potions. HP and gold are what pays for them.

- A relic acts in every fight left and takes no draw, so it is worth the fights left in which it acts: an Elite pays most in the first act and least in the final one. Elites are the relic source the route can choose; treasure, shops, and events add more.
- HP is needed to reach the next recovery and to enter each Boss fight with enough. HP beyond that is best spent on fights that pay: Elites for relics, normal fights for card rewards and gold.
- The first act is the cheapest place to trade HP for power. Its normal fights and Elites are the weakest of the run, and what they pay works in every later fight. Later Elites hit harder; a deck that grew in the first act can take them, one that avoided them meets the later acts with less.

Later acts' Bosses are stronger, and the final act may require two Bosses back to back with no recovery between. What a deck must answer is set out under Deck; route, shop, and rest choices serve the demand the fights show short.

# Rooms and route

- Route: first compare the paths to the act's Boss by what they pay: Elites, treasure, shops, unknown rooms, and rest sites, and the order they come in. Then check the preferred path against HP. Count the fights before each certain recovery and price each at what `fights` shows battles of its kind have cost this run; price an Elite not yet fought from its supplied rules when present, otherwise from the act's normal fights so far and the fact that it hits harder. A segment that does not fit within current HP is changed, or prepared for with a rest, gold, or potions, before entering. An Elite entered with HP for a bad opening and a recovery or easy rooms after it is normally worth its relic in the first act. In later acts, enter an Elite when the deck ends that act's normal fights in few turns with little HP lost and the HP left after it still covers the fights to the next recovery. In the final act, weigh any Elite against entering the Bosses with the HP they need. Do not always choose the safest path, and do not aim for a fixed HP threshold or number of Elites.
- Rest site: upgrade when HP covers the fights before the next recovery under plausible play, including an Elite the route is built around; rest when it does not, or when the recovered HP opens a better route. Upgrade the card whose upgrade most changes the fights ahead; where owned cards grow together, that is usually the one that raises their growth. Uncertain draws alone are not a reason to rest. When resting keeps displacing upgrades, the deck loses too much HP per fight; fix what the fights show to be short at the next deck change.
- Shop: compare the affordable combinations of cards, relics, removal, and potions with keeping the gold for a reachable later use. A bought card is judged like any added card, by the demand it answers, and removing a basic card, or a transition card once a core works, is often the best purchase. Buy a potion for a specific fight it changes.
- Alternative rewards, such as a choice of relics: HP restored between fights arrives after the damage is taken and does not shorten a fight or lower the damage inside it. Prefer what acts inside fights (energy, draw, damage, block, prevention), which lowers the HP lost in every fight left, unless current HP cannot survive the next fight without the healing. Charge a drawback the same way, in every fight left: a turn played without your choice takes that turn's damage unblocked.
- Events: charge a burden such as a curse, lost HP, or a lost card at its cost until a reachable remedy, and check that the next unavoidable fight stays survivable if a gamble fails.

After a battle report, change only what the evidence changes. One strong or weak fight is one observation, and a cause the report does not show is a hypothesis.
