# Map

`map.boss` identifies the known Bosses. `map.elite_pool` lists this act's candidate Elite encounters when known. `map.at` identifies the current node, its stretch, and its 1-based room number within that stretch; it is null before entering the act's first node. `map.next` is the set of destinations the game currently allows: null means availability is unknown, and an empty list means no destination is currently available. `map.elites_fought` lists this act's Elite battles so far, in order; it is absent when unknown.

A stretch has no fork inside: entering it commits to every room in it. `floors` gives its native act-local row or row range, and `rooms` lists the rooms in order. The room codes are M (Monster), E (Elite), R (rest site), $ (shop), T (treasure), ? (unknown), B (Boss), and A (Ancient). Each stretch's `next` lists its successor stretches. Stretch IDs are stable within the act. `unknown_odds` gives the native odds for unknown rooms, either once when they match or by stretch and 1-based room number when they differ; absent odds are unknown.

The current stretch ID in `map.next` means continue from the current room. A node ID in `map.next` is a special legal destination inside a stretch: only rooms from that entry point onward are committed. Use run_plan to replace the remaining plan with a sequence beginning at a destination in `map.next` and continuing through connected stretches. The plan may stop before the Boss and does not move. run_next enters the plan's next room; it requires a plan. A pending choice can block travel. After movement, read the actual position and replace an invalidated plan; automatic travel follows the game's legal destinations.

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
- A potion that adds random cards in combat, as one choice from several or several at once, draws them evenly from its pool, without the rarity weighting of card rewards and shops, so rare cards often appear among them.

Later acts' Bosses are stronger, and the final act may require two Bosses back to back with no recovery between. What a deck must answer is set out under Deck; route, shop, and rest choices serve the demand the fights show short.

# Weighing choices

- Distinguish when a benefit starts from how long it lasts. Healing and potions are spent once; upgrades, relics, removals, and good cards act in many fights, and an added card also takes a draw slot while it remains in the draw pool.
- Favor lasting strength while HP and potions cover plausible play along the reachable route. Buy short-term safety for a concrete danger, or for a worthwhile route it opens, that outweighs the lasting gain it displaces.
- Accept some weakness now for much greater attainable strength when the missing support is reasonably attainable, enough rewards remain, and the important fights before it works stay winnable.
- Spend scarce gold, upgrades, and rests on the change that alters outcomes most. Skipping, buying nothing, and saving are valid when the alternatives offer less.
- Shift from investment to realization as the run ends: with few fights left, favor what helps against the actual remaining fights and spend reserves when that raises the chance of winning.
- Count a required upgrade in a card's cost. Several cards cannot all claim the same upgrade.

Ask the questions under each room that fit the decision at hand. They direct attention; they are not a form to fill.

# Route

First compare the paths to the act's Boss by what they pay: Elites, treasure, shops, unknown rooms, and rest sites, and the order they come in. Then check the preferred path against HP:

- Count the fights before each certain recovery and price each at what `fights` shows battles of its kind have cost this run.
- An act's Elites come in rounds: each round holds every Elite of the act once, in random order, and a new round never opens with the Elite that closed the last one. Which Elite a room holds depends only on how many Elites this act has had, not on which room is entered, so the candidates are the Elites still unfought in the current round, and the last one of a round is certain. Before entering, test the deck against each candidate's supplied rules: what its first turns demand, and whether the deck has the answer its mechanism calls for, such as damage that kills several enemies together, block for its opening attacks, defense that holds while playing few of the cards it punishes, or output that does not feed what it punishes. Price the Elite at the worst candidate. A deck that clearly answers a certain candidate can enter it with less HP; a candidate the deck has no answer to needs HP for its bad case. Price it at the last fork where the route can still change, together with the forced rooms and recoveries after it, and weigh what avoiding it gives up: relics, rest sites, shops, and the normal fights the detour forces.
- A segment that does not fit within current HP is changed, or prepared for with a rest, gold, or potions, before entering.
- Before choosing a stretch, count its forced rooms from the entry point, including any Elite, and follow any only-successor stretches to the next choice; check whether current HP and resources reach the next recovery.

Elites by act:

- First act: an Elite entered with HP for a bad opening, and a recovery or easy rooms after it, is normally worth its relic.
- Later acts: enter an Elite when the deck ends that act's normal fights in few turns with little HP lost and the HP left after it still covers the fights to the next recovery. Route around an Elite whose rules the deck has no answer to, such as several enemies against a deck without damage to several.
- Final act: weigh any Elite against entering the Bosses with the HP they need.

Do not always choose the safest path, and do not aim for a fixed HP threshold or number of Elites.

- Which fights must be crossed to reach the extra rest site, shop, Elite, or reward, and can the deck survive until the next recovery?
- What can be seen on the shared part of the route before committing? Can the dangerous fight come after a known chance to improve, and what exit remains after a bad fight?
- Is the gap beating the Boss, or reaching it with enough HP and potions? Which reachable room can address it before then?
- Would spending HP now cause a later loss, or only reduce healing that would otherwise overflow?

# Rest sites

Upgrade when HP covers the fights before the next recovery under plausible play, including an Elite or Boss ahead; rest when it does not, or when the recovered HP opens a better route. On ordinary stretches, half HP or more is normally healthy; before a Boss—especially back-to-back final Bosses with no recovery between—judge HP against that Boss's expected damage and length instead of a fixed HP fraction. Missing HP, a wish for margin, or an imagined bad hand is not a reason to rest; count the block the deck actually draws and the potions held. Past battle losses are evidence, not a fixed cost of each future fight.

Upgrade the card whose upgrade most changes the fights ahead; where owned cards grow together, that is usually the one that raises their growth. When resting keeps displacing upgrades, the deck loses too much HP per fight; fix what the fights show to be short at the next deck change.

- What can the best upgrade change across the remaining fights: a kill turn, a block threshold, energy or draw, a target count?
- Which plausible hands before the next recovery does resting make survivable that upgrading does not?
- If another rest site comes before the main threat, is healing now and upgrading there better than the reverse?

# Shops

Compare the affordable combinations of cards, relics, removal, and potions with keeping the gold for a reachable later use. A bought card pays twice—gold and a draw slot—so hold shop cards to a higher bar than free rewards: do not buy discounted or secondary-synergy cards that would be marginal additions. Removing a basic starter (or an obsolete transition card once a core works) permanently raises the draw rate of every core, mitigation, and upgraded card in every remaining fight at zero energy and upgrade cost; when `deck_stats.basic_cards` or `deck_stats.cycle_turns` is high, removal is a top-priority purchase alongside key relics and decisive potions. Spending all gold is not a goal, and an empty slot is not a reason to buy a potion.

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
