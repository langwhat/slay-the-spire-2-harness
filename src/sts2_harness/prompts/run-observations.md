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

- A relic acts in every fight left and takes no draw or energy, so it is worth the fights left in which it acts: an Elite pays most in the first act and least in the final one. A relic that blocks or gives energy or draw also shortens the turns a deck spends being hit while its core arrives. Elites are the relic source the route can choose; treasure, shops, and events add more.
- HP is needed to reach the next recovery and to enter each Boss fight with enough. HP beyond that is best spent on fights that pay: Elites for relics, normal fights for card rewards and gold.
- The first act is the cheapest place to trade HP for power. Its normal fights and Elites are the weakest of the run, and what they pay works in every later fight. Later Elites hit harder; a deck that grew in the first act can take them, one that avoided them meets the later acts with less.
- Potions answer a particular dangerous fight or turn. The deck must win ordinary fights without them.

Later acts' Bosses are stronger, and the final act may require two Bosses back to back with no recovery between. What a deck must answer is set out under Deck; route, shop, and rest choices serve the demand the fights show short.

# Weighing choices

- Distinguish when a benefit starts from how long it lasts. Healing and potions are spent once; upgrades, relics, removals, and good cards act in many fights, and an added card also costs a draw in each of them.
- Favor lasting strength while HP and potions cover plausible play along the reachable route. Buy short-term safety for a concrete danger, or for a worthwhile route it opens, that outweighs the lasting gain it displaces.
- Accept some weakness now for much greater attainable strength when the missing support is reasonably attainable, enough rewards remain, and the important fights before it works stay winnable.
- Spend scarce gold, upgrades, and rests on the change that alters outcomes most. Skipping, buying nothing, and saving are valid when the alternatives offer less.
- Shift from investment to realization as the run ends: with few fights left, favor what helps against the actual remaining fights and spend reserves when that raises the chance of winning.
- Count a required upgrade in a card's cost. Several cards cannot all claim the same upgrade.

Ask the questions under each room that fit the decision at hand. They direct attention; they are not a form to fill.

# Route

First compare the paths to the act's Boss by what they pay: Elites, treasure, shops, unknown rooms, and rest sites, and the order they come in. Then check the preferred path against HP:

- Count the fights before each certain recovery and price each at what `fights` shows battles of its kind have cost this run. Price an Elite not yet fought from its supplied rules when present, otherwise from the act's normal fights so far and the fact that it hits harder.
- A segment that does not fit within current HP is changed, or prepared for with a rest, gold, or potions, before entering.
- Past the last fork before an Elite, the Elite is certain. Check it against current HP when choosing the fork, not later.

Elites by act:

- First act: an Elite entered with HP for a bad opening, and a recovery or easy rooms after it, is normally worth its relic.
- Later acts: enter an Elite when the deck ends that act's normal fights in few turns with little HP lost and the HP left after it still covers the fights to the next recovery. Route around an Elite whose rules the deck has no answer to, such as several enemies against a deck without damage to several. When several Elites are reachable, take first the ones the current deck answers best.
- Final act: weigh any Elite against entering the Bosses with the HP they need.

Do not always choose the safest path, and do not aim for a fixed HP threshold or number of Elites.

- Which fights must be crossed to reach the extra rest site, shop, Elite, or reward, and can the deck survive until the next recovery?
- What can be seen on the shared part of the route before committing? Can the dangerous fight come after a known chance to improve, and what exit remains after a bad fight?
- Is the gap beating the Boss, or reaching it with enough HP and potions? Which reachable room can address it before then?
- Would spending HP now cause a later loss, or only reduce healing that would otherwise overflow?

# Rest sites

Upgrade when HP covers the fights before the next recovery under plausible play, including an Elite the route is built around; rest when it does not, or when the recovered HP opens a better route. At half HP or more the reserve is normally healthy. Missing HP, a wish for margin, or an imagined bad hand is not a reason to rest; count the block the deck actually draws and the potions held. Past battle losses are evidence, not a fixed cost of each future fight.

Upgrade the card whose upgrade most changes the fights ahead; where owned cards grow together, that is usually the one that raises their growth. When resting keeps displacing upgrades, the deck loses too much HP per fight; fix what the fights show to be short at the next deck change.

- What can the best upgrade change across the remaining fights: a kill turn, a block threshold, energy or draw, a target count?
- Which plausible hands before the next recovery does resting make survivable that upgrading does not?
- If another rest site comes before the main threat, is healing now and upgrading there better than the reverse?

# Shops

Compare the affordable combinations of cards, relics, removal, and potions with keeping the gold for a reachable later use. A bought card is judged like any added card, by the demand it answers. Removing a basic card, or a transition card once a core works, is often the best purchase. Spending all gold is not a goal, and an empty slot is not a reason to buy a potion.

- Which affordable combination best answers the current gap? Does a relic offered fix it more lastingly than cards that work around it?
- After this purchase, what is lost at the next reachable shop, and can saved gold become strength before the next unavoidable fight?
- For a potion, which danger does it meet and which outcome could it change?

# Other rewards and events

Alternative rewards, such as a choice of relics: HP restored between fights arrives after the damage is taken and does not shorten a fight or lower the damage inside it. Prefer what acts inside fights (energy, draw, damage, block, prevention), which lowers the HP lost in every fight left, unless current HP cannot survive the next fight without the healing. Charge a drawback the same way, in every fight left: a turn played without your choice takes that turn's damage unblocked.

Events: charge a burden such as a curse, lost HP, or a lost card at its cost until a reachable remedy, and check that the next unavoidable fight stays survivable if a gamble fails.

- Which fights and resources will the burden affect before its remedy, and could income, a curse, or a route change remove that remedy?
- After a failed random attempt, is another attempt still worth its cost?

# After a battle report or other change

Change only what the evidence changes. One strong or weak fight is one observation, and a cause the report does not show is a hypothesis. Check a Combat review against the report, the deck, and the notes; a suggested card category does not restrict the solutions. Judge an earlier choice by what was known when it was made.

- Is the urgent gap still present? Which purchase, upgrade, or route plans are now obsolete?
- After an unknown room became a fight or a hoped-for supply failed to appear, does the route still work?
- Does a choice that helps short fights still work in the longer Boss fight?

Query card and relic knowledge when it could change a plan; supplementary knowledge is optional and its absence does not block play.
