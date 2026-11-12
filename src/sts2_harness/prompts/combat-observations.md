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
- Potions are consumed on use. Random draws, targets, and generated options are unknown until revealed.

# Choosing a line

At each decision, work in this order.

1. Threat. From the intents, add up the HP that would be lost at the next enemy action after current block, enemy by enemy and hit by hit. Note the enemy actions that grow, debuff, or add cards instead of attacking. Then read every enemy power and supplied rule for a deadline: a countdown, a defeat or escape condition, or growth the deck cannot outlast. Work out the turn it lands and exactly which cards or effects delay or cancel it, and where those cards are now.
2. Kill check. In play order, with energy gains, debuffs, and bonuses applied as they resolve, compute whether the hand and potions can kill an enemy before it acts. A kill cancels that enemy's action. So can a stun or a phase change; use the enemy's supplied rules for what it does afterward.
3. Candidate lines. Write out two or three materially different complete lines: remove the most dangerous attacker, prevent the damage, or bring the core's cards together. For each, list the plays in order with energy left after each, then enemy HP, own block, and HP lost when the enemies act.
4. Same horizon. Compare the lines through the same later enemy action, including the next hand each one leaves. Use known pile membership, draw count, and shuffle timing for the chance that needed cards arrive, and name the affordable fallback if they do not. Extend the horizon when a line's payoff or cost lands later.
5. Choose. A line that meets every deadline beats any line that does not, whatever HP it saves; never discard, exhaust, or leave unplayed a card that answers a deadline unless another answer is certain to arrive in time. Among lines that keep the fight won, choose the one that loses the least HP and resources over the rest of the fight. State any chance of death separately and weigh it before average HP.

These settle close cases:

- Damage that changes no enemy action and no later play is worth nothing, and HP spent for it is wasted. Block that only postpones defeat is not a line; compare the lines that can still win.
- Setup, growth, and clearing bad cards pay back over the remaining turns. Do them on quiet or already covered turns. Under attack, weigh the HP lost while setting up against the attacks the setup later prevents. Once output is enough to finish, finish.
- Order plays so each one sees the most. Reveal draws and random results before committing choices that depend on them, while the energy to act on them remains. Apply debuffs and buffs before the hits they scale. A draw that costs the energy needed to play what it finds is not access.
- Choose targets by the action their death prevents, not by HP. Crossing a threshold (a kill, a broken shield, a stun, a phase change) matters; damage short of it or beyond it matters only through later turns. Before triggering a phase change, compare the first turns of the next phase.
- Before exhausting, discarding, or transforming, check which attacks, defenses, and connectors remain through the kill. Spend cards outside the core and negative cards first, keeping the energy, draw, and block the line still needs. Keep the only copy of something the line needs unless spending it ends the fight.
- Limited triggers, counters, and stored resources are worth their later use, not their scarcity. Check how many remain and which play, including an automatic copy, would consume them.
- Use a potion where it changes the outcome most: a kill that cancels an attack, a turn that would cost much HP, a setup turn made safe, or a risk of death. Run's potion reservations are plans, not rules.
- Do not assume the best next hand, and do not demand certainty. A line that is better on average and survives its bad draws beats a safer line that loses more.
- A card's current version and cost are what the state shows; an upgrade or cost change in battle counts only after it resolves.
- After every reveal or completed step, recompute from the actual hand, energy, and powers before continuing.
