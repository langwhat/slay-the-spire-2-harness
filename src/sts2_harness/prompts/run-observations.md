`current.map_topology` gives the complete act map connections and publicly identified Bosses. `current.dynamic_map` gives the current position, complete native potential destinations, verified special-movement facts and the active remaining plan. `current.unknown_odds` gives the complete currently published per-node unknown-room odds. Each supplied map section replaces that entire section within the current game, run and act; omission retains the latest section only while that scope remains valid, including across node-reference resets. Unchanged map sections need not be repeated when `scene.reset=true`; they use stable map node IDs, not node-scoped object references. Initial observations and context baselines include the available map sections. A game, run or act change invalidates the previous map. Explicit null clears a whole section; empty and unknown values are distinct. The potential destinations do not by themselves mean travel is executable: the current choice and native travel state can block it. Ordinary connections and old ink do not establish current legality. Use run_inspect area=map to reread public map details.

Map planning results separately report plan acceptance, presentation and restoration. An accepted plan can coexist with unavailable, failed or uncertain presentation. If restoration is unconfirmed, submit no game actions or map-presentation calls until the program confirms the original interface and its controls are restored; a fresh observation alone does not prove this. Read-only queries remain available. Planning never moves or resolves a pending choice. Replanning replaces the remaining graph but does not erase old ink. A blocked run_next does not resolve a choice or select another destination. Automatic travel depends on all native legal destinations, not the selected planned edge. After movement, read the actual position and remaining plan; replace a plan whose next edge is absent, ambiguous or no longer legal.

# Run basics

These describe ordinary play. Apply native rules and the currently offered choices when they alter that flow.

- A run progresses through connected rooms and acts. Follow public map connections and currently reachable choices. Room types can include battles, elites, shops, rest sites, treasure, events, and Bosses. Unrevealed room contents and outcomes remain unknown.
- HP, max HP, gold, the permanent deck, relics, and remaining potions matter across rooms. Do not assume battle completion restores HP or other resources; use the returned state and the effects that actually apply.
- Adding, removing, transforming, and upgrading permanent deck cards can affect later battles. Battle piles are not the permanent deck: discarding or exhausting a card in battle is not permanent removal. Temporary and permanent changes are distinguished by native text and the observed deck.
- In battle, cards use their shown resources and affect HP, block, enemies, powers, or other cards. Cards are drawn from the top of the draw pile into the hand and normally move to discard after play or at turn-end cleanup; retention, exhaustion, and other effects can change that. When an allowed draw is needed and the draw pile is empty, the whole discard pile is shuffled into a new draw pile, removing those cards from discard retrieval. A card placed on top of the draw pile is not reserved for the next turn, and cards drawn or put into the hand before turn-end cleanup, including during automatic end-of-turn plays, are discarded unless retained or an effect's own timing differs. This circulation matters when judging deck changes, likely access to cards, and recorded sequencing plans.
- Resources, draw amounts, block retention, and power duration depend on game rules and effects. Consider native card text, relics, and powers together; do not assess a purchase or upgrade only by its printed base number.
- Rewards offer the choices shown. Take or decline only where that option exists. A full potion inventory can require a supported replacement or discard choice; do not assume capacity or automatic replacement.
- Shop prices and services are current facts. Purchases can spend resources, change later offers, or open a selection. Rest sites and events expose their actual available operations; some results are unknown until chosen.
- Potions have limited slots and are consumed on use. Use them outside battle only when the current state permits it.

# Planning and notes

Judge choices by their contribution to winning the whole run. Distinguish when a benefit starts from how long it lasts. Healing and consumable potions usually provide a one-time reserve; useful upgrades, relics, and deck improvements can contribute across many fights. A permanent addition can also impose a recurring cost.

1. Favor lasting strength when current reserves can cover plausible play along the reachable route. At healthy HP, choose a useful upgrade over ordinary healing, including before an elite or Boss. Depart from this default for a specific likely survival shortfall or a concrete route or resource benefit that outweighs the upgrade. A large refill or a healing relic bonus does not establish that benefit by itself. Count useful upgrade payoffs in later acts as well as the coming fight. Extra margin and the ordinary uncertainty of drawing the upgraded card are not enough. Value an upgrade by its plausible uses and changed outcomes; a benefit need not be guaranteed to count. Buy short-term safety when it addresses a concrete danger or enables a worthwhile route that outweighs the lasting improvement or future purchase it displaces.

2. Accept some immediate weakness for much greater attainable upside. Invest before every component is present when the required support is reasonably attainable, enough opportunities remain, and the transition cost is acceptable. Reduce that investment's value when it needs several scarce components, competes for limited upgrades, or substantially worsens important fights before it works. Missing support is uncertain, not automatically worthless or guaranteed to arrive.

3. Require small improvements to justify their recurring costs. A card being stronger than a starter, cheap, or compatible with one bonus is not enough. Take transition cards when they fill a meaningful current gap; raise the bar for similar additions once that gap is covered. Include the useful draws they displace now and the stronger cards they may dilute later.

4. Allocate scarce resources to the improvement that changes outcomes most. Compare purchases, upgrades, and resource reservations with their best alternative uses. Favor important gaps, decisive thresholds, or substantially greater gains over additions that are merely useful. Buying nothing, skipping, and saving resources are valid when their alternatives offer less value.

5. Shift from investment to realization as the run ends. With many fights remaining, value repeated contributions and attainable growth. With few fights remaining, favor benefits that help against the actual remaining threats and spend reserves when doing so improves the chance of winning. Discount plans needing acquisition or upgrade opportunities that no longer remain. A decisive final upgrade can still beat healing; act number alone does not decide the choice.

6. Evaluate upgrade dependence and include the required upgrade in the acquisition cost. Distinguish a large upgrade benefit, needing the upgrade to perform the intended role, and deserving the next upgrade over existing cards. Invest in a transformative upgrade when it can arrive in time; discount a weak base version when upgrade access is scarce or contested. A useful unupgraded card can still justify itself.

Compare all offered alternatives, including skipping a reward, buying nothing, and smaller purchase sets. Each addition must beat adding nothing. A useful card, a shared theme, or an existing bonus is not enough on its own.

## Before a reward or other deck change

Reassess the construction plan at meaningful rewards, purchases, and upgrades. Compare the strongest serious alternatives, including skipping, using this order:

1. Establish the required scale and timing from the remaining threats: when damage, defense, growth, or control must become sufficient. Use known HP, intents, interference, and resource limits; state relevant uncertainty rather than inventing a fixed damage or block target. A timely kill can remove an attack, while defense or control can buy more growth time. Keep damage, block, and survival effects distinct rather than adding them into one score.
2. Find promising engines from the actual effects of owned cards and relics: accumulating resource -> amplification or conversion -> useful payoff, with access and reuse connecting the steps. Look for ways the same initial turn energy can yield increasing output as battle progresses. Check native details when an interaction matters. Also consider worthwhile development routes, explicitly identifying unowned support, remaining acquisition or upgrade opportunities, and the cost of surviving until completion.
3. Derive how the output grows. Let n count actual feasible growth activations, not assumed turns. A resource S = S0 + g*n converted at a fixed rate gives linear growth. Two independently growing factors multiplied together can give quadratic growth. A retained result repeatedly multiplied by q > 1 can give exponential growth. A fixed doubling only changes a coefficient; accumulating several turns of damage is not the same as increasing damage per turn. Rank useful output before the enemy's deadline, not the name or degree of the curve. A strong fixed-output line may already meet the threat.
4. Work through a complete affordable turn or short sequence after the necessary setup. Check each prefix's energy and available cards, effect order, hand space, once-per-turn triggers, temporary modifiers, retained resources, and card recovery. Count the setup cost separately from each growth and payoff use. For repeated draw/energy cycles, track what is recovered and what is depleted on every cycle, including HP and exhausted cards. Credit feasible extra cycles without assuming an infinite loop. Finite-use resources can be saved for later; compare them with reusable resources over the same relevant fight duration and actual spending opportunities.
5. Compare the candidate's effect on growth rate, startup time, useful payoff, and the frequency of affordable payoff turns. Include plausible hands with the core available, late, or absent. Adding a card also occupies draws and may consume energy, hand space, retrieval targets, random-play slots, or the first amplified trigger. Even an unplayed card can delay the engine. A small direct effect can be a valuable connector; a growth card can be excess setup when payoff or access is the actual bottleneck.
6. Choose the improvement that best advances a sufficient, attainable line while covering concrete transition risks. A developing engine need not already be dependable to guide acquisitions. Compare how remaining choices can repair its constraints instead of freezing its value at today's access rate. Require an off-plan addition to cover a specific important shortfall or establish a better route, with enough benefit to outweigh dilution and competing uses. The printed sum of damage and block, being stronger than a starter, or matching one bonus is not sufficient. Revise the route when its premises or remaining opportunities change.

### Contrasting mechanisms

These are hypothetical comparisons, not card rankings or assumed game effects. Apply the actual costs, rules, and threats.

- Payment threshold: with 3 energy, a 3-cost defense followed by a 1-cost conversion cannot be paid manually; making the conversion free unlocks the combined turn. With 4 available energy the original sequence already fits, so the upgrade's gain is the useful action enabled by the saved energy. In both cases, check component access and the best competing upgrade.
- Growth and conversion: suppose a repeatable effect adds 2 to S, block is 4*S, the relevant block gain is doubled, and an attack reads that block. Each growth activation then adds 16 block and 16 damage to a later qualifying payoff, not merely 2 printed points. Include the activation cost and any enemy benefit. Doubling every qualifying block gain changes the conversion factor; S still increases additively, so payoff remains linear in growth activations. Exponential growth would require retaining and repeatedly multiplying the accumulating quantity itself.
- Draw versus payment: a 1-cost card that draws one normally replaces its own hand slot while spending energy. With spare energy and useful play effects it may improve access; when paying it leaves the newly drawn core unaffordable, it can obstruct the turn. Extra energy likewise needs available useful actions to spend it on. Compare the completed sequence, not the draw or energy keyword alone.
- Transition versus dilution: an extra 7 block can matter if it closes an otherwise uncovered 7-damage survival gap before setup. If affordable existing defense already covers that window, the same addition needs another concrete contribution to justify delaying growth or payoff cards. A missing component can justify early investment when opportunities and reserves remain; the same route loses value when those opportunities are gone.

### When the candidate adds damage, defense, or another copy

- Which actual damage or defense actions receive the proposed bonus?
- How often can they trigger it before it matters?
- Does the next threat require faster output, different targets, or a particular hit pattern?
- Can the current direct damage, lasting damage, or retaliation already win?
- Will the proposed defense remain available until the dangerous turn?
- Would more block, more draw, carried block, or defense independent of this turn's draw cover the gap?
- Which option leaves enough energy for setup?
- How many cards, energy, and upgrades would a second damage plan require?
- Which defense or access would it displace?
- Do random targets or enemy responses to extra plays undermine it?
- Does the plan still have a way to beat each publicly identified possible Boss?
- After expanding the deck, can the answer to a specific threat still arrive in time?
- Does another copy stack, share a counter or decay clock, improve access, or repeat a dependable role?
- How often can the extra copy contribute over the relevant turns?
- Would drawing or reusing an existing copy provide more?
- What later stronger acquisition would this addition also dilute?
- Is another card that spends a permanent resource needed for earlier access or for two actual uses?
- Can ordinary fights be handled without spending that resource?

An existing damage bonus does not justify every compatible attack. An existing defensive effect does not rule out another source that compounds it. When a duplicate or interaction drives a choice, name it in the short action reason.

### Before generating, retaining, removing, or replacing cards

- By the critical turn, what is the net deck change after exhaustion and continued generation?
- What does a negative card cost on its first draw before it can be cleared?
- Does the current plan need compression or a continuing supply of cards to consume?
- Can generated cards be drawn and played in time?
- Do they separate resource producers from consumers or delay defense?
- Can existing retrieval already use the original output?
- Does retention preserve useful components or trap weak cards in hand?
- What energy and hand space does clearing those retained cards require?
- Are several effects consuming the same hand, statuses, or expendable cards?
- After one effect uses that supply, what remains for the others?
- Is the whole deck weak, or is one specific function missing?
- Would a broad replacement destroy the only defense, output, or fuel before repairs are available?
- If the deck is duplicated, can fixed draw still reach its core in time?
- What opening actions would innate cards displace?
- Is the duplication adding independent strong plays or repeating weak cards and waiting?
- Which removable card blocks a critical draw or empty-hand effect at the worst time?
- Which weak card can existing effects handle safely?
- Does adding, removing, or making a card innate change a retrieval target, random upgrade pool, or first trigger?

Usually prioritize weak starter attacks and blocks for removal, while checking actual eligibility, synergies, and opportunity costs. Compare draw quality rather than deck size alone. A larger deck can dilute weak cards or delay essential ones. Evaluate the offered version; rarity and upgrade status are not automatic rankings.

Weak basic Strikes and Defends, such as CARD.STRIKE_IRONCLAD and CARD.DEFEND_IRONCLAD, can dilute stronger draws. Exhausting those cards during a battle is an important benefit: they stop entering later shuffles, so useful attacks, defenses, and engine cards cycle more often. Value that improvement across the remaining turns and fights when judging an exhaust effect or relic. Do not label losing a weak basic a cost merely because it leaves the battle piles. Count a loss only when its remaining output, defense, or specific synergy is still needed; exhaustion does not permanently remove it from the deck.

## At a shop

- Does the card justify both its permanent deck slot and its gold cost?
- Which affordable combination of cards, relics, removal, and potions best addresses the current gap?
- Does the proposed combination add a capability the deck does not already supply reliably?
- Is buying an extra item for an acquisition trigger worth the full price?
- After this purchase or removal, which options at the next known reachable shop are lost?
- Does the HP saved by spending now outweigh the later purchase opportunity?
- Can saved gold become useful strength before the next mandatory threat?
- If the next shop has no affordable solution, can the current deck pass that threat?
- If an offer buys information, which concrete action could that information change?
- Does it reveal only the current pile or also a later unknown order?
- Is the avoided loss worth the alternative direct improvement?

Use actual prices and triggers. Keep the whole purchase plan affordable and reassess after each reveal or change. Spending all available gold is not a goal.

Before committing gold, compare the strongest affordable relic alternatives with the proposed card and potion purchases. Use available knowledge to verify relic effects or interactions that could change the plan. A relic that repeatedly clears weak draws can improve access to many strong cards without occupying a draw slot; compare that lasting improvement with buying extra draw or exhaust cards to work around the same weak draws.

For a potion purchase, identify the danger it is meant to address and the outcome it could change, including a later Boss fight. Compare that reserve with the opportunities saved gold could buy. An affordable price, an empty slot, or leftover gold is not sufficient reason to purchase. A potion that makes an otherwise unsafe valuable route viable can be worth more than saving.

## At a rest site or before taking an upgrade-dependent card

Compare what the current and upgraded versions can actually do. Look for changes in target count, net draw and energy efficiency, duration across relevant turns, and setup, kill, or defense thresholds. Count the card's own draw slot, energy cost, and play requirements. One upgraded card may replace multiple unupgraded plays while leaving energy and draws for other actions; compare the full effects and triggers rather than assuming those plays are equivalent. A larger printed number or percentage alone does not establish the gain.

If the intended role depends on upgrading, include how soon a suitable upgrade is reachable and which existing upgrade or necessary rest it consumes. Several dependent cards cannot all claim the same upgrade opportunity. Favor a large functional improvement when it can contribute before the relevant threats; discount it when the base version is weak and the required upgrade is unlikely to arrive in time. Compare upgrade targets by actual changed plays: extra draw needs usable resources, more targets need useful eligible cards, and longer duration needs actions that benefit before it expires.

- What can the card do before it is upgraded?
- Which reachable upgrade would make it useful before the next threat?
- Which existing upgrade or necessary rest would that consume?
- If several cards rely on one in-battle upgrade effect, can it reach all of them in time?
- How often will the upgrade matter during dangerous turns?
- Which kill, defense, or setup timing does it change?
- Does it still help when its supporting card is absent?
- Which plausible late-core hands does resting make survivable?
- Does stronger recovery actually remove a later need to rest?

Compare healing and upgrading across the reachable route. HP buys survival and useful routes; it is not a goal in itself. If another rest site precedes the main threat, compare healing now and upgrading there with upgrading now and healing there. Include the intervening fights, healing lost to the HP cap, and the possible second upgrade. Choose rest when its actual recovery improves survival along the route or preserves other resources enough to outweigh the best upgrade. State that tradeoff in the short reason. Count each rest site as healing or upgrading unless its actual rules allow both. Do not value a future upgraded card as already upgraded.

Before choosing rest, identify the best eligible upgrade from the actual card versions and compare its useful changes across the remaining fights with the actual HP recovered, including relic bonuses and the HP cap. Do not dismiss all upgrades as small merely because each changes one card. Early and mid-run, an upgrade can contribute on many later draws, while ordinary healing is spent once.

At half HP or above, treat the current reserve as healthy by default and normally forge a useful upgrade. Rest needs a concrete benefit on the reachable route that outweighs that upgrade. Missing HP, a general desire for more margin, hypothetical unlucky hands, or preserving ordinary potions does not establish that benefit by itself; available potions and other recovery are part of the survival reserve. This is not a healing cutoff: recovery that can usefully be spent on the remaining threats, especially against a weak upgrade with few remaining uses, can justify resting. With healthy HP, compare the extra HP likely to matter across the remaining threats with the best upgrade's cumulative contribution. Account for later healing and the HP cap instead of comparing a full refill with one use of the upgrade. Compare those real effects rather than following a blanket preference for either action.

Judge survival using plausible play with the current deck, the proposed upgrade, and usable reserves. Do not require an upgrade or potion to guarantee safety against every draw before crediting its value. Naming a Boss attack or imagining a bad hand does not show that a healthy reserve is insufficient. Count the block that the actual deck can afford and draw; an attack being blockable does not mean it will be fully blocked, and its printed damage is not automatically an HP loss. Prefer the useful upgrade unless a realistic uncovered damage shortfall or concrete use for the recovery outweighs its contribution; uncertainty alone is not that shortfall.

Past battle losses are evidence of what happened, not a fixed cost of each future fight. Account for the next encounters, improved play, a stronger upgrade, and usable potions before projecting those losses forward. A forecast that merely leaves less HP is not the same as a likely inability to reach the next recovery opportunity.

## At a map fork or before reserving resources across fights

- Which actual fights must be crossed to reach the extra rest site, shop, or reward?
- Can the deck survive until recovery?
- After expected travel losses, how much of that recovery remains useful?
- What rewards or information can be seen on the shared route before committing?
- Can the dangerous fight be placed after a known chance to improve?
- What exit remains after a bad fight?
- Before a long-term plan works, which mandatory threats must already be beaten?
- How many acquisition opportunities actually remain before them?
- Is the gap beating the Boss or reaching it with enough HP and potions?
- Which available reward type can address that gap before the deadline?
- Can a plan requiring repeated copies, upgrades, or accumulation work with the minimum remaining opportunities?
- How does the partly completed plan survive along the way?
- Which rests or ordinary upgrades does it displace?
- If a limited-fight bonus is used now, which battles receive it?
- Can delaying it save a refill and still cover the critical fight?
- Can the intervening fights, including possible unknown-room battles, be survived without it?
- Before the next definite recovery, how much HP must remain for other fights and poor draws?
- Would spending HP now cause a later loss or only reduce healing that would otherwise overflow?
- After spending HP or max HP for strength, can the next fight survive a late core?

Compare reachable threats, rewards, recovery, remaining special moves, and fallback routes. Value flexibility when rewards are similar without always choosing the safest path. Use the public Boss identity and supplied guidance; keep unobserved mechanics unknown. Do not use fixed HP thresholds or elite counts.

## At an event that charges a cost or creates a burden

- Before the burden can be removed, which resources and fights will it affect?
- What reachable action removes it?
- Could income, a curse, or a route change invalidate that remedy?
- After a failed random attempt costs HP, is another attempt still worth its cost?
- After another failure, could the next unavoidable fight still be survived?

## After an acquisition, loss, reveal, or battle report

- Is the previously urgent gap still present?
- Which old purchase, reward, upgrade, or route plans are now obsolete?
- Is another similar card filling a remaining need or adding setup burden?
- What job does an early transition card still perform?
- Has a new interaction made a card reliable enough to cancel its planned removal?
- After an unknown room becomes a fight or a hoped-for supply fails to appear, does the route still work?
- Does a choice that helps short fights still work in the longer required Boss fight?

Check battle reviews against the report, retained observations, current deck, and notes. A suggested card category does not restrict the solutions. Keep unsupported causes unknown. Preserve findings that change future decisions, not claims unsupported by missing history.

## When resolving offers and recording the decision

Treat separate reward groups as separate choices. Assess the combination left after earlier picks and resolve each group. Earlier actions can change resources, options, references, and legality. Combine supported actions according to run_act's execution and recovery rules.

Use native details and supplied guidance. Query missing public details when they could change the decision. Supplementary knowledge is optional; its absence does not block play. Explicit knowledge and skill reads return the requested full content even if previously seen. Apply guidance to the current run rather than treating it as a ranking.

Use run_act's comparison in the short reason: identify the changed capability or constraint, its useful scale when supported, and the gain over skipping or the strongest alternative. For a decline, explain why the offered improvement does not outweigh its costs to the current or developing engine. Justify distinct choices in separate reward groups; keep the explanation concise rather than reporting every planning step.

## When updating strategy notes

When run_notes is available, keep one current construction plan under its tool contract. Reassess it when an acquisition, upgrade, newly known threat, or battle finding changes the engine or its constraints, including during an act task or handoff. Combat receives an opening copy, so preserve the practical order and conditions for growth and payoff, not only acquisition preferences. Use supported battle evidence to distinguish access, payment, slow setup, insufficient output, and execution mistakes; change the construction priority that addresses that cause. A sequence need not have occurred in the last battle to be mechanically useful, and one successful payoff does not establish reliable coverage.

Preserve useful resource reservations and their reasons. Potion reservations are revisable plans: reconsider them when the expected battle outcome and later opportunity cost favor using the potion. Keep notes concise by replacing obsolete conclusions rather than appending encounter narratives. Follow run_notes' subject-identification and complete-proposition editing rules.

You may freely query card knowledge to form or revise the plan, starting with your character's card pool and extending to relevant other pools. Use run_knowledge catalog and pool-filtered, paginated search to examine the full pool and look up promising interactions. Query details that could change the plan; future possibilities are not owned cards or guaranteed rewards.
