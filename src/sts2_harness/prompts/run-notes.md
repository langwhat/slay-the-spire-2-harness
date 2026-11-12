# Strategy notes

Keep the deck's core in the strategy notes, which Combat reads at the start of each battle.

`core` lists at most five owned cards and relics: sources, what triggers them, and payoffs. It is not an inventory of the deck: energy, draw, and block cards stay out, and so does any card that works without the others. Record the strongest group as soon as two owned cards or relics are linked, in any act; until then leave it empty. Add a card taken to start a stronger group, and rewrite `core` when that group becomes the stronger one.

`plan` states the core's turn in at most 300 characters: its cards in play order with their total cost against that turn's energy, the battle turn by which they are normally drawn and paid for, the damage on that turn and two turns later, and what the core still lacks, a missing half first. Exhausted cards and powers already played no longer occupy draws. Check the figures against the HP and damage of the act's Elites and Boss, and of the final act's Bosses when supplied knowledge describes them. A core whose cost to its owner grows each turn must win before that cost does. While `core` is empty, `plan` says what the act's Elites and Boss still demand of the deck.

After each battle report, rewrite `plan` from what limited the fight. Core cards arrived late or were not drawn: fewer cards and more draw. The core was ready and the fight still ran long: growth or damage. The fight ended on time and cost HP: block. A fight that cost a fifth of max HP or more stays in `plan`, with its cause, until the deck answers it.
