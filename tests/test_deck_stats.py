from pydantic import MISSING
from test_mod_client import CARD, COMBAT, RUN

from sts2_harness.client import models as native
from sts2_harness.deck_stats import DeckStatistics


def observation(*costs: int | native.CostSymbol | None) -> native.Observation:
    result = native.Observation.model_validate_json(RUN)
    assert isinstance(result.state, native.RunState)
    card = native.CardEntry.model_validate_json(CARD)
    return result.model_copy(
        update={
            "state": result.state.model_copy(
                update={
                    "deck": tuple(f"c{i}" for i in range(len(costs))),
                    "player": result.state.player.model_copy(
                        update={"base_draw": 2, "base_energy": 3}
                    ),
                }
            ),
            "objects": {
                f"c{i}": card.model_copy(
                    update={
                        "definition_id": f"CARD.{i}",
                        "cost": native.ResourceCosts(energy=cost),
                    }
                )
                for i, cost in enumerate(costs)
            },
        }
    )


def history(*cards: str) -> native.HistoryData:
    return native.HistoryData(
        cards_played_this_combat=tuple(
            native.NamedDefinition(name=card, definition_id=card) for card in cards
        ),
        cards_played_this_turn=(),
        cards_exhausted_this_turn=(),
    )


def test_current_deck_counts_are_independent_of_battle_history() -> None:
    current = observation(0, 1, 3, native.CostSymbol.X)
    statistics = DeckStatistics()
    stats, uses = statistics.view(current)
    assert stats == {
        "cards": 4,
        "attacks": 4,
        "skills": 0,
        "powers": 0,
        "basic_cards": 0,
        "curses_and_statuses": 0,
        "unplayable": 0,
        "circulating": 4,
        "basic_and_curse_pct": 0.0,
        "circulating_avg_energy": 1.33,
        "base_draw": 2,
        "cycle_turns": 2.0,
        "base_energy": 3,
        "added": 0,
        "skipped": 0,
    }
    assert uses == {}
    battle = native.Observation.model_validate_json(COMBAT)
    statistics.capture(battle, history("CARD.0", "CARD.0", "CARD.1"))
    statistics.capture(battle, history("CARD.0", "CARD.0", "CARD.1"))
    after, uses = statistics.view(current)
    assert after == stats
    assert uses == {"CARD.0": 2, "CARD.1": 1}
    assert DeckStatistics().view(current)[0] == stats


def test_average_energy_counts_only_playable_circulating_copies_with_fixed_cost() -> None:
    current = observation(1, 1, 3, 8, 8, 8, 8, native.CostSymbol.X, native.CostSymbol.NONE, None)
    objects = dict(current.objects)
    for ref, updates in {
        "c1": {"definition_id": "CARD.0"},
        "c3": {"card_type": "power"},
        "c4": {"keywords": ("exhaust",)},
        "c5": {"keywords": ("unplayable",)},
        "c6": {"playable": False},
    }.items():
        card = objects[ref]
        assert isinstance(card, native.CardEntry)
        objects[ref] = card.model_copy(update=updates)

    stats, _ = DeckStatistics().view(current.model_copy(update={"objects": objects}))
    assert stats["circulating"] == 8
    assert stats["circulating_avg_energy"] == 1.67
    no_fixed_cost, _ = DeckStatistics().view(observation(native.CostSymbol.X, None))
    assert "circulating_avg_energy" not in no_fixed_cost


def test_circulating_excludes_each_one_shot_card_once_and_choices_reset_each_act() -> None:
    current = observation(1, 2, 0, native.CostSymbol.NONE)
    objects = dict(current.objects)
    for ref, updates in {
        "c0": {"card_type": "power", "keywords": ("exhaust",)},
        "c1": {"keywords": ("exhaust",)},
        "c2": {"keywords": ("unplayable", "ethereal")},
    }.items():
        card = objects[ref]
        assert isinstance(card, native.CardEntry)
        objects[ref] = card.model_copy(update=updates)
    assert isinstance(current.state, native.RunState)
    current = current.model_copy(
        update={
            "objects": objects,
            "state": current.state.model_copy(
                update={
                    "player": current.state.player.model_copy(update={"base_draw": 4}),
                }
            ),
        }
    )
    statistics = DeckStatistics()
    statistics.record_choice(current, True)
    statistics.record_choice(current, False)
    stats, _ = statistics.view(current)
    assert stats["circulating"] == 1
    assert stats["unplayable"] == 1
    assert (stats["added"], stats["skipped"]) == (1, 1)
    next_act = current.model_copy(
        update={
            "state": current.state.model_copy(
                update={
                    "progress": current.state.progress.model_copy(update={"act": 2}),
                }
            )
        }
    )
    next_stats, _ = statistics.view(next_act)
    assert (next_stats["added"], next_stats["skipped"]) == (0, 0)


def test_basic_cards_and_cycle_turns_reflect_rarity_and_base_draw() -> None:
    current = observation(1, 1, 1, 1, 1, 1, 1)
    objects = dict(current.objects)
    for ref, updates in {
        "c0": {"rarity": "basic"},
        "c1": {"rarity": "BASIC"},
        "c2": {"rarity": "common"},
        "c3": {"rarity": "uncommon"},
        "c4": {"rarity": "rare", "card_type": "power"},
        "c5": {"rarity": None},
    }.items():
        card = objects[ref]
        assert isinstance(card, native.CardEntry)
        objects[ref] = card.model_copy(update=updates)
    assert isinstance(current.state, native.RunState)
    with_draw = current.model_copy(
        update={
            "objects": objects,
            "state": current.state.model_copy(
                update={"player": current.state.player.model_copy(update={"base_draw": 4})}
            ),
        }
    )
    stats, _ = DeckStatistics().view(with_draw)
    assert stats["cards"] == 7
    assert stats["circulating"] == 6
    assert stats["basic_cards"] == 2
    assert stats["cycle_turns"] == 1.5

    empty_circulating = with_draw.model_copy(
        update={"state": with_draw.state.model_copy(update={"deck": ("c4",)})}
    )
    empty_stats, _ = DeckStatistics().view(empty_circulating)
    assert empty_stats["circulating"] == 0
    assert empty_stats["cycle_turns"] == 0.0

    zero_draw = with_draw.model_copy(
        update={
            "state": with_draw.state.model_copy(
                update={"player": with_draw.state.player.model_copy(update={"base_draw": 0})}
            )
        }
    )
    zero_stats, _ = DeckStatistics().view(zero_draw)
    assert zero_stats["basic_cards"] == 2
    assert "cycle_turns" not in zero_stats

    missing_draw = with_draw.model_copy(
        update={
            "state": with_draw.state.model_copy(
                update={"player": with_draw.state.player.model_copy(update={"base_draw": MISSING})}
            )
        }
    )
    missing_stats, _ = DeckStatistics().view(missing_draw)
    assert "cycle_turns" not in missing_stats


def test_curses_statuses_and_basic_and_curse_pct_track_junk_dilution() -> None:
    current = observation(1, 1, None, None, None, 1)
    objects = dict(current.objects)
    for ref, updates in {
        "c0": {"rarity": "basic"},
        "c1": {"rarity": "common"},
        "c2": {"card_type": "curse", "rarity": "curse", "keywords": ("unplayable",)},
        "c3": {"card_type": "status", "rarity": "status", "keywords": ("unplayable",)},
        # Unplayable + ethereal curse is counted in curses_and_statuses, not in circulating.
        "c4": {"card_type": "curse", "rarity": "curse", "keywords": ("unplayable", "ethereal")},
        "c5": {"card_type": "power", "rarity": "rare"},
    }.items():
        card = objects[ref]
        assert isinstance(card, native.CardEntry)
        objects[ref] = card.model_copy(update=updates)
    stats, _ = DeckStatistics().view(current.model_copy(update={"objects": objects}))
    assert stats["curses_and_statuses"] == 3
    assert stats["circulating"] == 4
    # Circulating cards: c0 (basic), c1 (common), c2 (curse), c3 (status) -> 3/4 = 75.0%
    assert stats["basic_and_curse_pct"] == 75.0

    # When circulating is 0, basic_and_curse_pct is omitted.
    assert isinstance(current.state, native.RunState)
    no_circulating = current.model_copy(
        update={
            "objects": objects,
            "state": current.state.model_copy(update={"deck": ("c4", "c5")}),
        }
    )
    empty_stats, _ = DeckStatistics().view(no_circulating)
    assert empty_stats["curses_and_statuses"] == 1
    assert empty_stats["circulating"] == 0
    assert "basic_and_curse_pct" not in empty_stats


def test_card_type_normalization_is_case_insensitive_across_all_counts() -> None:
    current = observation(1, 1, 2, None, None)
    objects = dict(current.objects)
    for ref, updates in {
        "c0": {"card_type": "ATTACK", "rarity": "common"},
        "c1": {"card_type": "Skill", "rarity": "common"},
        "c2": {"card_type": "POWER", "rarity": "rare"},
        "c3": {"card_type": "CURSE", "rarity": "curse", "keywords": ("unplayable",)},
        "c4": {"card_type": "Status", "rarity": "status", "keywords": ("unplayable",)},
    }.items():
        card = objects[ref]
        assert isinstance(card, native.CardEntry)
        objects[ref] = card.model_copy(update=updates)
    stats, _ = DeckStatistics().view(current.model_copy(update={"objects": objects}))
    assert stats["attacks"] == 1
    assert stats["skills"] == 1
    assert stats["powers"] == 1
    assert stats["curses_and_statuses"] == 2
    assert stats["circulating"] == 4
    assert stats["basic_and_curse_pct"] == 50.0


def test_observe_records_post_battle_cards_played_including_one_turn_kills_and_final_turn() -> None:
    statistics = DeckStatistics()
    battle_turn1 = native.Observation.model_validate_json(COMBAT)
    # Mid-combat snapshot misses the killing turn on node "node".
    statistics.capture(battle_turn1, history("CARD.0"))

    post_battle = observation(1, 1)
    assert isinstance(post_battle.state, native.RunState)
    # Post-battle RunState on the same node includes the killing turn and overwrites the sample.
    settled = post_battle.model_copy(
        update={
            "state": post_battle.state.model_copy(
                update={
                    "battle_cards_played": (
                        native.NamedDefinition(name="Card 0", definition_id="CARD.0"),
                        native.NamedDefinition(name="Card 1", definition_id="CARD.1"),
                        native.NamedDefinition(name="Card 1", definition_id="CARD.1"),
                    )
                }
            )
        }
    )
    statistics.observe(settled)
    _, uses = statistics.view(settled)
    assert uses == {"CARD.0": 1, "CARD.1": 2}

    # A 1-turn kill with no capture() call still records via observe().
    one_turn_kill = settled.model_copy(
        update={
            "location": settled.location.model_copy(update={"node_id": "node-1tk"}),
            "state": settled.state.model_copy(
                update={
                    "battle_cards_played": (
                        native.NamedDefinition(name="Finisher", definition_id="CARD.FINISHER"),
                    )
                }
            ),
        }
    )
    statistics.observe(one_turn_kill)
    # Non-battle RunState omitting battle_cards_played does not disturb the window.
    shop = post_battle.model_copy(
        update={"location": post_battle.location.model_copy(update={"node_id": "shop"})}
    )
    statistics.observe(shop)
    _, uses = statistics.view(shop)
    assert uses == {"CARD.0": 1, "CARD.1": 2, "CARD.FINISHER": 1}

    # 5-battle rolling window evicts oldest battle once a 6th battle is observed.
    for index in range(4):
        extra = settled.model_copy(
            update={
                "location": settled.location.model_copy(update={"node_id": f"extra-{index}"}),
                "state": settled.state.model_copy(
                    update={
                        "battle_cards_played": (
                            native.NamedDefinition(name="Extra", definition_id="CARD.EXTRA"),
                        )
                    }
                ),
            }
        )
        statistics.observe(extra)
    _, uses = statistics.view(shop)
    assert uses == {"CARD.FINISHER": 1, "CARD.EXTRA": 4}


def test_execution_observes_post_battle_cards_played_automatically() -> None:
    import asyncio

    import httpx2 as httpx

    from sts2_harness.client import ModClient
    from sts2_harness.execution import Execution
    from sts2_harness.game import LiveGame

    run_obs = observation(1)
    assert isinstance(run_obs.state, native.RunState)
    post_battle = run_obs.model_copy(
        update={
            "state": run_obs.state.model_copy(
                update={
                    "battle_cards_played": (
                        native.NamedDefinition(name="Strike", definition_id="CARD.STRIKE"),
                        native.NamedDefinition(name="Bash", definition_id="CARD.BASH"),
                    )
                }
            )
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content='{"status":"completed","observation":' + post_battle.to_json() + "}",
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        stats = DeckStatistics()
        async with ModClient("http://mod", transport=httpx.MockTransport(handler)) as client:
            game = LiveGame(
                client,
                namespace="test",
                game_instance_id="game",
                run_id="run",
                wait_for_start=False,
            )
            execution = Execution(
                game,
                game_instance_id="game",
                run_id="run",
                deck_stats=stats,
            )
            observed = await execution.observe()
            _, uses = stats.view(observed)
            assert uses == {"CARD.STRIKE": 1, "CARD.BASH": 1}

    asyncio.run(scenario())
