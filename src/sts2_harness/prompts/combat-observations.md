# Battle rules

Read every value from the current state; these rules cover what the state does not spell out.

- Energy, costs, block, and hand are whatever the state shows; nothing resets to an assumed amount. An X cost follows its card's rule. Some resources persist across turns.
- Effects that count card types use the native type only: an Attack that also gains block is an Attack. A bonus triggered by a play applies from the moment it resolves, not to effects already resolved. A card that cannot be played now can still be a selection candidate.
- Block stops damage, not HP loss. Target previews and enemy intents already include the modifiers active when shown; adjust only for what changes afterward, and count every hit of a multi-hit intent.
- An enemy at 0 HP may revive or change phase. The battle has ended only when the state says so.
- A power's amount may be an intensity, a duration, a charge count, or a counter. Read its rule before assuming it decays or lasts.
- A draw takes the top card of the draw pile, within hand capacity and any draw restriction. If a draw is still allowed and needed when the draw pile is empty, the whole discard pile is shuffled into a new draw pile first; a card still resolving is not in it. Emptying the draw pile alone does not shuffle. A card placed on top is taken by the next draw, including one during turn end. Pile lists show membership, never order. Putting a card into the hand by another effect is not a draw.
- Turn end runs in this order: automatic end-of-turn plays and their triggers, then end-of-turn-in-hand effects, then discarding every unretained hand card, including cards drawn during that sequence.
- Retain does not suppress an end-of-turn-in-hand effect (for example CARD.BURN, CARD.DECAY, CARD.DOUBT, CARD.SHAME). An Ethereal card with such an effect resolves it and then exhausts; Ethereal exhausts at turn end even when retained. The Exhaust keyword applies only when the card is played.
- Battle piles are not the permanent deck. Cards created, changed, or exhausted in battle are temporary unless their text says otherwise.
- A character without base orb slots gains one orb slot when it channels an orb while its current orb capacity is 0.
- Potions are consumed on use. Random draws, targets, and generated options are unknown until revealed.

# Choosing a line

At each decision, work in this order.

1. Threat. From the intents, add up the HP that would be lost at the next enemy action after current block, enemy by enemy and hit by hit. Note the enemy actions that grow, debuff, or add cards instead of attacking. Then read every enemy power and supplied rule for a deadline: a countdown, a phase change, a defeat or escape condition, or growth the deck cannot outlast. Work out the turn it lands, which cards or effects delay or answer it, and where those cards are now.
2. Kill check. In play order, with energy gains, debuffs, and bonuses applied as they resolve, check whether the hand and potions can kill an enemy before it acts. A kill cancels that enemy's action. So can a stun or a phase change; use the enemy's supplied rules for what it does afterward.
3. Lines. Find the materially different lines: remove the most dangerous attacker, prevent the damage, or bring the core's cards together. Compare them through the same later enemy action, including the next hand each one leaves. Use known pile membership, draw count, and shuffle timing for the chance that needed cards arrive, and know the affordable fallback if they do not. Extend the horizon when a line's payoff or cost lands later.
4. Choose. A line that meets every deadline beats any line that does not, whatever HP it saves; never discard, exhaust, or leave unplayed a card that answers a deadline unless another answer is certain to arrive in time. Among lines that keep the fight won, choose the one that loses the least HP and resources over the rest of the fight. State any chance of death separately and weigh it before average HP.

These settle close cases:

- Damage that changes no enemy action and does not bring any kill closer, such as overkill, is worth nothing, and HP spent for it is wasted. In a long fight, damage on an enemy that must die is never wasted: the total decides the turn it dies, and each turn earlier removes that turn's attacks. Block that only postpones defeat is not a line; compare the lines that can still win.
- When a line takes blockable damage, check what it loses if its least useful plays are replaced with block or prevention, through the same enemy action.
- Setup, growth, and clearing bad cards pay back over the remaining turns. Do them on quiet or already covered turns. Under attack, weigh the HP lost while setting up against the attacks the setup later prevents. Once output is enough to finish, finish, unless a safe delay sets up a relic counter as below.
- A relic counter whose text does not limit it to a turn or a battle carries into the next battle. While the remaining enemy actions are covered, extra plays or turns that bring such a counter just short of its trigger make it fire sooner next battle; avoid spending the trigger on a play that changes nothing here. Weigh the delay against the HP, potions, and enemy growth it costs, and finish once the counter is set or the delay stops being safe.
- Order plays so each one sees the most. Reveal draws and random results before committing choices that depend on them, while the energy to act on them remains. Apply debuffs and buffs before the hits they scale. A draw that costs the energy needed to play what it finds is not access.
- Choose targets by the action their death prevents, not by HP. Crossing a threshold (a kill, a broken shield, a stun, a phase change) matters; damage short of it or beyond it matters only through later turns.
- Do not assume the best next hand, and do not demand certainty. A line that is better on average and survives its bad draws beats a safer line that loses more. Use estimates or ranges where exact chances are unknown; do not invent precision.
- A card's current version and cost are what the state shows; an upgrade or cost change in battle counts only after it resolves. A relic, power, or bonus counts only when the current state shows it active.
- After every reveal or completed step, recompute from the actual hand, energy, and powers before continuing: whether the planned continuation still works, whether a definite solution has appeared, and whether another target, another potion, or no further optional play is now better.

# Questions for the situation

Ask the questions that fit the decision at hand. They direct attention; they are not a form to fill.

## Before a kill, stun, or phase change

- Which target's death cancels the most dangerous coming action?
- If a retained kill card is spent now, what handles the next threat?
- Does the extra damage cross a shield or other threshold that stops an action or enables the next play?
- After the stun or phase change, which enemies attack on each coming turn, and what answers the first attack of the next phase?
- Would delaying the transition keep the needed block, debuff, or card available for that attack?
- Can a minion whose death changes the fight be left at killable HP while damage goes to the main enemy, and is that safe from automatic damage killing it early?
- Does killing by a play, by end-of-turn damage, or by retaliation change when the next phase or an ally acts? Is that timing supplied, or only assumed?

Use the supplied enemy rules for death and phase timing; never infer them from the sequence you want. When a transition starts a large attack or a countdown, trigger it on a turn whose hand, or the next hand, can still block or prevent that attack; until then, spend energy on block, debuffs, or setup that will be ready for it.

## Before drawing, retrieving, or generating cards

- Which possible results fill the current damage, defense, or setup gap, and what can still be paid for after the access is paid for?
- Is the need for more cards, or for particular cards together on an affordable turn?
- What useful effect is lost by discarding or exhausting the input card?
- What does drawing or putting a card on top now move ahead of a needed card?
- How many hand slots will the action and its triggers need, and where do overflow cards go?
- Will a retrieval target be in an eligible pile when the retrieval resolves?

Seeing more cards is not the payoff. Spend on access when its likely results beat the best line already in hand, counting the energy left to use them. Value a retrieved or top-decked card at its actual arrival, with the energy, durations, and piles then present.

## Before committing to the next hand or a reshuffle

- Which known and newly created cards, including negative ones, enter the next shuffle, after exhaustion, retention, and discards?
- Which plausible next hands can kill or defend after each candidate line?
- If the needed card does not arrive, what affordable sequence remains, and does it survive the next attack that matters?
- When a line takes damage to deal more now, at the current pace how many enemy attacks remain before the kill, can ordinary next hands cover them with the HP left, and how much more does each extra turn cost as the enemy grows?
- Does drawing or shuffling now remove a later retrieval target?
- When replacing the whole hand, which retained defenses or key cards are lost?

When death is a possible result, keep its chance and the loss of the run apart from ordinary HP loss; the surviving branches alone do not describe the line. Damage capped by remaining HP does not make death cheaper.

## Before clearing cards, setting up, or extending a loop

- Is this a safe turn to clear cards that would otherwise take energy or draws at the critical moment?
- After the next exhaust or persistent cost, which attacks, defenses, and connecting cards remain through the kill, and where are the indispensable ones?
- Can the loop start if a key connector arrives late, and continue after paying each cycle's energy and clearing costs?
- Is enemy pollution arriving faster than it can be cleared?
- How much HP is lost before dependable defense resumes, and can the remaining piles bridge that gap?
- If a risky play fails to find its combination, how long can retention, retrieval, and potions keep the line alive?

Before exhausting, discarding, or transforming, spend cards outside the core and negative cards first, keeping the energy, draw, and block the line still needs. Prefer a continuation that keeps working over a local gain that removes its only necessary card; spend that card when doing so finishes the fight or another line can take over its role. For an in-battle upgrade, compare the plays it enables now and on later uses with its energy and timing cost.

## Before spending a persistent resource or limited trigger

- What later damage or defense is lost by spending it now, and does the immediate result replace that or finish the fight in time?
- How many triggers remain under the limit, and will this play or an automatic copy use the one a key card needs?
- Which small hit or status damage could consume a one-use defense before the dangerous hit, and what must be blocked or cleared first?
- If a revive or death-prevention effect triggers, on which hit, and is the HP left enough for the hits after it?
- How many layers of an enemy's debuff protection must be removed before the debuff that matters takes effect?

Keep a limited resource for its actual later use, not for its scarcity. Two cards showing the same one-use bonus cannot both consume it.

## Before using or keeping a potion

- Which failed draws become survivable with it, and which required card no longer has to arrive?
- Can a useful draw or random action reveal its result first, while the potion window remains?
- How many times can its effect contribute during the enemies' actual attacks and quiet turns?
- What specific later fight justifies keeping it, and what HP or slot does keeping it cost now?

Use a potion where it changes the outcome most: a kill that cancels an attack, a turn that would cost much HP, a setup turn made safe, or a risk of death. Earlier lethal and HP saved can justify use before HP becomes critical. Run's potion reservations are plans, not rules.

## Before a random action or an optional extra turn

- Which cards or effects can the random action select, and could it consume energy, HP, limited triggers, or the existing line?
- Can the counter, resources, or eligible pool be adjusted first?
- If a known kill or defense already works, what does more searching or delaying the finish actually gain, against the extra enemy actions it allows?

Spare energy or a playable card is not a benefit by itself; delaying a finish needs a real gain.
