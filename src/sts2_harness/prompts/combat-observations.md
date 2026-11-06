# Battle basics

These describe ordinary play. Apply the actual card, keyword, relic, power, and enemy rules when they alter that flow.

- A player turn gives opportunities to play cards, use permitted items, and resolve choices before ending the turn. Turn transitions resolve game effects and enemy actions and prepare the next player turn. Read current energy, hand, and other resources instead of assuming fixed starting amounts.
- Cards spend their displayed resources. Some use resources other than energy. X is interpreted using that card's rules, not as zero. Resource retention and reset depend on the resource and effects; some resources persist across turns. A card that cannot currently be played can still be a valid selection candidate. Effects that count or require card types use each card's native type, not its other effects: an Attack that also gains block counts only as an Attack. A play-triggered bonus applies from when its trigger resolves, not to effects that already resolved.
- Block protects against damage according to the effect's rules; HP loss and damage are not interchangeable. Read block after transitions instead of assuming it persists. Native target previews account for the modifiers included in that preview at that observation. They are not necessarily final HP loss: block, defenses, triggers, and later changes still matter.
- Enemy intents describe announced actions. Displayed intent damage is the game's current per-hit value, including modifiers already applied at that observation; adjust only for changes after it. For multi-hit intents, account for every hit and intervening effects. Do not assume a fallen enemy or zero HP proves the battle or run has ended when revival or phase changes may apply; use the returned phase and outcome.
- Buffs, debuffs, and other powers may use amounts as strength, duration, charges, or counters. Read their rules; do not assume every amount decays or lasts indefinitely.
- Drawing moves cards from the draw pile to the hand, subject to draw restrictions and hand capacity. Putting a card into the hand by another effect is not a draw: it follows that effect's rules and hand capacity, and draw triggers or draw restrictions apply only if their rules also cover it. Played cards normally go to discard after resolving. At normal turn end, automatic end-of-turn plays and their triggers resolve before end-of-turn hand effects and before unretained hand cards are discarded; cards drawn or put into the hand before that cleanup are discarded with it unless retained. Effects explicitly deferred until after cleanup follow their own timing. Retention, exhaustion, card types, and other effects can change that flow or trigger additional effects.
- Retain does not suppress a card's end-of-turn-in-hand effect. Cards with such effects (for example CARD.BURN, CARD.DECAY, CARD.DOUBT, and CARD.SHAME) resolve them before going to discard, even with Retain or whole-hand retention. If such a card also has Ethereal, its effect resolves before it exhausts; exhaustion-triggered benefits come afterward. Exhausting it before turn end removes it from that hand-effect sequence. Ethereal cards without a hand-end effect exhaust at turn end even when retained. The Exhaust keyword alone applies when played, not merely when left in hand; follow the card's actual effect and keywords rather than its Status or Curse type.
- An allowed draw takes the current top card of the draw pile. If another draw is still allowed and needed when the draw pile is empty, the entire current discard pile is shuffled into a new draw pile, and those cards are no longer discard-retrieval targets. Emptying the draw pile alone does not shuffle, and a requested draw that is prevented or stopped by hand capacity does not force one. Placing a card on top of the draw pile does not reserve it for the next turn: the next allowed draw takes it, including one triggered during the turn-end sequence, and it then arrives as an ordinary draw; later top placements or shuffles can also move it. Non-hand pile arrays show membership, not hidden draw order. Do not assume a card still resolving is already in discard for that shuffle.
- The permanent deck and the battle piles are different. Moving or exhausting a battle card is not a permanent deck removal. Cards created or modified for a battle are not automatically permanent changes. Use native text and the observed deck to distinguish temporary and persistent effects.
- Potions occupy limited slots and are consumed when used. Availability and targeting come from the current game state. Random draws, targets, and generated options remain unknown until revealed.

# Planning a sequence

Choose executable lines by their probability-weighted outcomes toward winning the run. Compare expected total HP loss and resource use through the same relevant enemy actions, extending the comparison when setup, growth, or later draws change the result. Accept damage now when the improved continuation outweighs it. Extra damage that does not improve the continuation does not justify avoidable HP loss. Defense that only postpones defeat is not a winning line.

Use the questions for the decision at hand. They guide planning; they are not an answer checklist.

## Before choosing a line

- What can the current hand, resources, intents, and public piles actually support?
- Are the powers and bonuses this line needs already active?
- What is the strongest materially different line, including a different target, selection, retrieval, or setup?
- Can each action be paid for when it occurs, including resources gained earlier in the sequence?
- After every hit, HP cost, trigger, and delayed effect, how much HP does each line actually lose?
- If a line takes blockable damage, what happens if its least useful plays are replaced with effective prevention?

Use current previews and announced intents without applying their included modifiers twice. Block produced is not necessarily HP saved. Separate guaranteed, plausible, and unavailable continuations. Estimate probabilities only when they affect the choice.

If neither line kills now, compare both through the same later enemy action that could cause HP loss. Include intervening player turns and quiet enemy actions. Calculate the damage needed before that action, including enemy block, growth, and still-active modifiers. Use known pile membership, draw counts, actual shuffle timing, and any known top-deck order to assess what those turns can afford. If the difference occurs later, extend both lines through that attack, setup payoff, or loss of useful draws. Earlier lethal alone saves no HP when the skipped actions would cause no HP loss; include the growth, pollution, resource costs, and bad-draw risk it avoids.

## Before choosing a kill, stun, or phase transition

- Which target's death cancels the most dangerous upcoming action?
- If a retained kill card is spent now, what handles the next threat?
- Does extra damage cross a shield or other threshold that actually stops an action or enables the next play?
- After a stun or phase change, which enemies attack on each coming turn?
- Does reducing this turn's peak leave a safe setup turn, or require defense on every turn?
- What handles the first attack of the next phase?
- Would delaying the transition leave the needed defense, debuff, or card access available?
- With energy gains, debuffs, and extra damage applied in order, is lethal already available?

Favor damage, control, or setup that crosses a threshold changing an enemy action or a later executable play. Choose targets by the pressure their removal prevents, not HP alone. Once a threshold is met, compare further investment with defense, another target, or preparation; include any actual benefit from excess damage or block. A phase transition can expose a new threat, so compare the resulting turns before triggering it.

Use the supplied enemy rules for death and phase timing. Do not infer those rules from the desired sequence.

## Before drawing, retrieving, or generating cards

- Which possible results fill the current damage, defense, or setup gap?
- After paying for access, what can still be played?
- What useful effect is lost by discarding or exhausting the input card?
- Is the need for more cards, or for particular components together on an affordable turn?
- Will resource producers arrive before their consumers need them?
- What does drawing or top-decking now move ahead of a needed component?
- What reliable progress is lost by stopping draws to keep a card?
- Is spending this connecting card now worth losing its protection against a stalled next turn?
- How many hand slots will the action and its extra triggers need?
- Where will overflow cards go?
- Will a retrieval target be in an eligible pile when retrieval resolves?

Spend resources on access when its possible results improve the executable turn or continuation enough to beat the best line already in hand. Include the chance of finding the needed effect and the resources left to use it. If access spends the energy or components needed to play its result, prefer the existing line unless triggers or later cycling justify that cost. Access can improve plays, resources, triggers, or later cycling; seeing more cards is not itself the payoff. Value a retrieved or top-decked card at its actual arrival, with the resources, status durations, and pile contents then available.

## Before committing to the next hand or reshuffle

- Which known cards and newly created cards enter the next shuffle?
- Which negative cards return with them?
- How do exhaustion, retention, discards, and automatic returns change that pool?
- Which plausible next hands can kill or defend after each candidate line?
- If the needed card does not arrive, what affordable sequence remains?
- Can that sequence survive the next meaningful attack?
- Does drawing or shuffling now remove a later retrieval target?
- When replacing the whole hand, which retained defenses or components are lost?
- How do the replacement hand's probability-weighted outcomes compare with keeping the current protection?

Do not assume the best possible hand or require guaranteed next-turn lethal from every setup. Compare possible next hands using probabilities supported by known pile membership, draw counts, and shuffle timing. For each branch, include the feasible response after the result is revealed, including a change of target or potion use. Compare probability-weighted HP loss and remaining resources over the same horizon. Do not prefer a line merely because it is certain, or reject a better expected outcome merely because a worse draw is possible. Use estimates or ranges when exact probabilities are unavailable; do not invent precision.

When death is a possible result, distinguish its probability and the loss of the continuation from ordinary HP loss; surviving branches alone do not describe the whole line. Do not let damage capped by remaining HP make death look cheaper, and do not invent a fixed death penalty or a blanket worst-case preference. If defensive lines only postpone defeat, compare the outcomes of the attacking or setup lines that can still win.

## Before clearing cards, setting up, or extending a loop

- Is this a safe turn to clear cards that would consume energy or draws at the critical moment?
- If the clearing effect is spent now, what handles later pollution?
- After the next exhaustion or persistent consumption, which attacks, defenses, and connectors remain through lethal?
- Where are the indispensable cards when that consumption resolves?
- Can the loop start if a key connector arrives late?
- Can it continue after paying each cycle's energy and pollution-clearing costs?
- Is a backup card preserving a fallback or occupying the opening draw that starts the loop?
- Is enemy pollution arriving faster than it can be cleared?
- Does adding a usable card dilute dangerous statuses or delay the cards that keep the loop alive?
- Among adding cards, clearing cards, and killing sooner, which preserves useful actions?
- Does taking damage to set up or attack now avoid a concrete later attack?
- How much HP is lost before dependable defense resumes?
- Can the remaining pile and available alternatives bridge that gap?
- Does another growth or delayed-damage play change lethal timing or release later energy?
- What defense or connection does that investment displace?
- If a risky play fails to find its strong random combination, how long can known retention, retrieval, and potions keep the line alive?
- If ordinary defense only delays defeat, which line still has a credible win?

Invest in setup, growth, or clearing when its later contribution outweighs the actions and HP spent before it pays off. Quiet or already defended turns often provide a cheaper window. Under pressure, compare the expected losses during setup with the later attacks or resource costs it avoids. Once output is sufficient to finish, further growth must improve the continuation enough to justify delaying the finish.

Use each card's actual current version. For an in-battle upgrade, compare the actions enabled now and on later uses with the upgrade's energy and timing cost. Include the useful eligible cards it affects and what remains affordable afterward. Do not count the improved effects before the upgrade resolves, or count only isolated printed increases when upgrading several cards changes the whole turn.

Before exhausting cards or extending a loop, account for the attacks, defenses, and connectors left through the end of the fight. Prefer a sustainable continuation over a local gain that removes its only necessary component. Spend that component when doing so finishes the fight or another available line can take over its role.

## Before spending persistent resources or limited triggers

- What later damage or defense is lost by spending this persistent resource now?
- Can the immediate result replace that contribution or finish the fight in time?
- How many triggers or actions remain under the current limit?
- Will this play or an automatic copy consume the opportunity needed by a key card?
- What counter will be left for the next critical turn?
- If energy, duplication, or a bonus is saved, will a suitable card, target, and debuff still be available?
- Which small hit or status damage could consume a one-use defense before the dangerous hit?
- What must be blocked or cleared first to preserve that defense?

Preserve a limited resource for its actual later contribution, not merely because it is scarce. Spend it when the immediate result and remaining continuation outweigh that contribution. Use the specific rules for resource retention, capacity, and release. Two cards displaying the same one-use bonus cannot both consume it.

## Before using or reserving a potion

- Which failed draws become survivable with this potion?
- Which required card no longer has to arrive?
- Does a copying or replay effect have a suitable original and an affordable follow-up?
- Can an already useful draw or random action reveal its result before choosing a potion?
- After that action, will the survival or lethal line still have its energy, components, and potion window?
- How many times can the effect contribute during the enemies' actual attacks and quiet turns?
- How should the available defenses cover those different turns?
- What specific later gap justifies keeping this potion?
- What HP loss or slot pressure does keeping it impose now?

Use a potion at the window where its effect most improves the outcome relative to saving it. Earlier lethal, cheaper setup, expected HP saved, or reduced dependence on a particular draw can justify use before HP becomes critical. Preserve it when current plays handle the fight well and its later opportunity is more valuable. Treat Run's potion reservations as revisable plans. Compare the current benefit with the later need and the alternatives available in each fight.

## Before a random action or an optional extra turn

- Which cards or effects can the random action actually select?
- Which energy, HP, or limited triggers can they consume?
- Could they dismantle the existing line?
- Can the counter, available resources, or eligible pool be adjusted first?
- If a known kill or defense already works, what gap does more random searching fill?
- Can the known solution be reused without exposing it to an immediate negative effect?
- What does delaying the finish actually gain in recovery, potions, triggers, or stored resources?
- What extra enemy actions and resource costs does that delay require?
- What is the expected gain from the replacement after accounting for the known resource spent and possible outcomes?

Resolve a useful information-producing action before committing dependent choices when the information can improve the decision and the required energy, components, and action window remain available. Include the resource cost and possible negative effects of obtaining that information. Reassess after the reveal.

Once a satisfactory line is available, continue searching or taking extra actions only when their expected improvement outweighs their costs and possible disruption. A known line is an alternative to compare, not an automatic reason to stop. Spare energy or a playable card is not itself a benefit; delaying a finish needs an actual gain worth the extra enemy actions and resource costs.

## After a reveal or completed action

- Do the actual hand, energy, triggers, and active powers still support the planned continuation?
- Is a definite solution now available?
- Does the new state call for another target, a different potion, or no further optional actions?

Earlier steps change later resources, costs, bonuses, targets, and piles. Combine actions, anticipated selections, and follow-up plays when public information supports them, following combat_act's execution and recovery rules.

In combat_act.reason, when the batch is not lethal and lines differ materially, give in one or two short lines, for the chosen line and its main alternative, the expected total own HP loss from now through that same later enemy action, counting that action if it is not prevented, and the chance and HP cost of missing the kill before it. Mention a material difference in death risk or resource use separately rather than hiding it inside an HP average. Use estimates or ranges where uncertain; do not invent exact values or narrate the full analysis.
