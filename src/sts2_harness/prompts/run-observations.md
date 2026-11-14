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

# Rooms and route

- Shop: compare the affordable combinations of cards, relics, removal, and potions with keeping the gold for a reachable later use. A bought card is judged like any added card, and removing a basic card, or a transition card once a core works, is often the best purchase. Buy a potion for a specific fight it changes.
- Rest site: upgrade when HP covers the fights before the next recovery under plausible play; rest when it does not, or when the recovered HP opens a better route. Upgrade the card whose upgrade most changes the fights ahead; where owned cards grow together, that is usually the one that raises their growth. Uncertain draws alone are not a reason to rest. When resting keeps displacing upgrades, the deck loses too much HP per fight; fix what the fights show to be short at the next deck change.
- Route: plan through the next certain recovery, counting the fights and possible unknown-room battles before it. Price each at what `fights` shows battles of its kind have cost this run; an Elite or Boss not yet fought costs several times a normal fight. When that total exceeds current HP, change the route, rest, or spend gold and potions on it before entering. Take the more dangerous branch when its rewards outweigh the HP it is expected to cost and a fallback remains. Elites and Bosses are the fights the deck must be built for. Enter an Elite only when the deck has an answer to every Elite the act can produce and HP to spare for its worst opening; otherwise route around it and strengthen the deck first.
- Alternative rewards, such as a choice of relics: HP restored between fights arrives after the damage is taken and does not shorten a fight or lower the damage inside it. Prefer what acts inside fights (energy, draw, damage, block, prevention), which lowers the HP lost in every fight left, unless current HP cannot survive the next fight without the healing. Charge a drawback the same way, in every fight left: a turn played without your choice takes that turn's damage unblocked.
- Cards that an event or relic lets the deck add are judged like any added card, one at a time. When any number may be chosen, each chosen card must pass on its own, and none is a valid choice.
- Events: charge a burden such as a curse, lost HP, or a lost card at its cost until a reachable remedy, and check that the next unavoidable fight stays survivable if a gamble fails.

After a battle report, change only what the evidence changes. One strong or weak fight is one observation, and a cause the report does not show is a hypothesis.
