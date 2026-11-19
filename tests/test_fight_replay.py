import asyncio
from contextlib import closing
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError
from test_combat import reply
from test_lifecycle import Campaign, terminal
from test_mod_client import CARD
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.fights import Fight, FightHistory, FightKind
from sts2_harness.game import Admitted, GameReply, LiveGame
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.note_values import NotesState
from sts2_harness_eval.decision_cases import DecisionLabels, DecisionSource, TaskEvent
from sts2_harness_eval.decision_extract import extract_case
from sts2_harness_eval.decision_replay import rebuild_case


@pytest.mark.parametrize(
    ("scenario", "expected"),
    (
        ("known", "ENCOUNTER.OLD"),
        ("missing_encounter", None),
        ("missing_id", None),
        ("mismatched_summary", None),
        ("missing_observation", "ENCOUNTER.OLD"),
        ("same_floor", None),
        ("same_node", None),
        ("other_run", None),
    ),
)
def test_reconstructed_decision_recovers_only_proven_fight_encounters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, expected: str | None
) -> None:
    class SourceFightHistory(FightHistory):
        def observe(self, observation: native.Observation) -> None:
            super().observe(observation)
            self.recent = tuple(
                fight.model_copy(update={"encounter": None}) for fight in self.recent
            )
            if scenario == "mismatched_summary":
                self.recent = tuple(
                    fight.model_copy(update={"hp_in": fight.hp_in + 1}) for fight in self.recent
                )
            if scenario == "missing_observation" and len(self.recent) == 1:
                self.recent += (
                    Fight(
                        floor=4,
                        kind=FightKind.NORMAL,
                        hp_in=8,
                        hp_out=7,
                        damage=(),
                        damage_one=(),
                        incoming=(),
                        hp_lost=(),
                        potions=(),
                    ),
                )
            if scenario == "same_node":
                self.recent = self.recent[-1:]

    monkeypatch.setattr("sts2_harness.execution.FightHistory", SourceFightHistory)

    async def run() -> None:
        campaign = Campaign()
        opening = campaign.states[1]
        if scenario not in ("missing_encounter", "other_run"):
            encounter = (
                native.NamedDefinition(name="古老敌人")
                if scenario == "missing_id"
                else native.NamedDefinition(name="古老敌人", definition_id="ENCOUNTER.OLD")
            )
            opening = opening.model_copy(update={"encounter": encounter})
        reward = campaign.states[2]
        state = reward.state
        assert isinstance(state, native.RunState)
        reward = reward.model_copy(
            update={
                "stamp": reward.stamp.model_copy(update={"observation_sequence": 3}),
                "objects": {"c1": native.CardEntry.model_validate_json(CARD)},
                "state": state.model_copy(
                    update={
                        "progress": state.progress.model_copy(update={"floor": 1}),
                        "battle_turns": 4,
                        "selection": native.Selection(
                            prompt="Choose",
                            source=None,
                            min=1,
                            max=1,
                            candidates=("c1",),
                            selected=(),
                        ),
                    }
                ),
            }
        )
        responses = [
            reply(("plan", "run_plan", '{"reason":"Go.","stretches":["s2"]}')),
            reply(("travel", "run_next", '{"reason":"Go."}')),
            reply(("fight", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(text="Review."),
        ]
        if scenario in ("same_floor", "same_node"):
            bridge = reward.model_copy(
                update={
                    "state": reward.state.model_copy(
                        update={
                            "selection": None,
                            "actions": (native.Action(ref="advance", label="Next", enabled=True),),
                        }
                    )
                }
            )
            combat = opening.state
            assert isinstance(combat, native.CombatState)
            second = opening.model_copy(
                update={
                    "stamp": opening.stamp.model_copy(update={"observation_sequence": 4}),
                    "location": opening.location.model_copy(
                        update={"node_id": "battle1" if scenario == "same_node" else "battle2"}
                    ),
                    "state": combat.model_copy(
                        update={"player": combat.player.model_copy(update={"hp": 9})}
                    ),
                    "encounter": (
                        native.NamedDefinition(name="古老敌人", definition_id="ENCOUNTER.OLD")
                        if scenario == "same_node"
                        else native.NamedDefinition(
                            name="另一个敌人", definition_id="ENCOUNTER.OTHER"
                        )
                    ),
                }
            )
            reward = reward.model_copy(
                update={
                    "stamp": reward.stamp.model_copy(update={"observation_sequence": 5}),
                    "state": reward.state.model_copy(
                        update={"player": state.player.model_copy(update={"hp": 8})}
                    ),
                }
            )
            campaign.states = [
                campaign.states[0],
                opening,
                bridge,
                second,
                reward,
                terminal(reward),
            ]
            responses.extend(
                [
                    reply(
                        ("advance", "run_act", '{"steps":[{"kind":"interact","action":"advance"}]}')
                    ),
                    reply(("fight2", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                    reply(text="Second review."),
                ]
            )
        else:
            campaign.states = [campaign.states[0], opening, reward, terminal(reward)]
        responses.append(reply(("pick", "run_act", '{"steps":[{"kind":"select","refs":["c1"]}]}')))
        campaign.responses = iter(responses)
        if scenario == "other_run":
            foreign_opening = opening.model_copy(
                update={
                    "location": opening.location.model_copy(update={"run_id": "other-run"}),
                    "encounter": native.NamedDefinition(
                        name="古老敌人", definition_id="ENCOUNTER.OLD"
                    ),
                }
            )
            foreign_reward = reward.model_copy(
                update={"location": reward.location.model_copy(update={"run_id": "other-run"})}
            )

            class ForeignObservationGame(LiveGame):
                injected = False

                async def admit(self) -> GameReply[Admitted]:
                    result = await super().admit()
                    if self.injected or not result.observations:
                        return result
                    self.injected = True
                    return result.model_copy(
                        update={
                            "observations": (foreign_opening, foreign_reward, *result.observations)
                        }
                    )

            monkeypatch.setattr("sts2_harness.live_session.LiveGame", ForeignObservationGame)
        archive = tmp_path / "source.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="fight-replay",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(campaign.model)),
        ) as session:
            await session.run()
        with closing(Journal.open(archive)) as journal:
            sequence = journal.requests()[-1].sequence
        source = DecisionSource(
            round="fixture", seed="seed", floor=1, model_sequence=sequence, archive=archive
        )
        case = await extract_case(source, DecisionLabels(comment="fixture"))
        task = next(event for event in reversed(case.events) if isinstance(event, TaskEvent))
        assert len(task.state.fights) == (
            2 if scenario in ("missing_observation", "same_floor") else 1
        )
        encounter = task.state.fights[0].encounter
        if expected is None:
            assert encounter is None
        else:
            assert encounter is not None
            assert encounter.definition_id == expected
            assert encounter.name == "古老敌人"
        if scenario in ("missing_observation", "same_floor"):
            assert task.state.fights[1].encounter is None
        async with Llm(configuration(tmp_path)) as client:
            rebuilt = await rebuild_case(case, client)
        body = TypeAdapter(dict[str, JsonValue]).validate_python(rebuilt.prepared.body)
        messages = body["input"]
        assert isinstance(messages, list)
        observations: list[dict[str, JsonValue]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            value = message.get("content")
            if not isinstance(value, str):
                continue
            try:
                content = TypeAdapter(dict[str, JsonValue]).validate_json(value)
            except ValidationError:
                continue
            observation = content.get("observation")
            if isinstance(observation, dict):
                observations.append(observation)
        delivered = next(item["fights"] for item in reversed(observations) if "fights" in item)
        assert isinstance(delivered, list) and isinstance(delivered[0], dict)
        assert delivered[0]["encounter"] == (
            {"definition_id": expected, "name": "古老敌人"} if expected is not None else None
        )

    asyncio.run(run())
