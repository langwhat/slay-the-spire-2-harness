from contextlib import closing
from pathlib import Path

import pytest
from test_assessment import write_step
from test_lifecycle import battle_state, run_state
from test_responses import configuration

from sts2_harness.capacity import CapacityPolicy
from sts2_harness.client import models as native
from sts2_harness.game import GameReply, Observe, Observed
from sts2_harness.journal import (
    HeadlessInitialConditions,
    Journal,
    Manifest,
    NativeStartReceipt,
    SessionMaterials,
    StandardCharacter,
)
from sts2_harness.journal_models import ConfigurationSnapshot, ToolSet
from sts2_harness.note_values import NotesState
from sts2_harness_eval.cases import (
    AdmissionFailure,
    AdmissionRequest,
    AfterDecisionsSpec,
    TargetBattle,
    plan_admission,
)
from sts2_harness_eval.results import ModelAnchor


@pytest.mark.parametrize("skip", [False, True])
def test_reward_plan_stops_after_one_confirmed_choice_without_absorbing_later_actions(
    tmp_path: Path, skip: bool
) -> None:
    archive, anchor = reward_source(tmp_path, skip=skip)
    plan = plan_admission(
        AdmissionRequest(
            id="reward", archive=archive, anchor=anchor, endpoint=AfterDecisionsSpec(count=1)
        ),
        tmp_path,
    )
    assert len(plan.suffix) == 1
    assert len(plan.follow_on) == 1
    assert plan.follow_on[0].steps[0].step == native.Select(
        kind=native.StepKind.SELECT, refs=() if skip else ("card:2:1",)
    )
    assert plan.follow_on[0].after.input_readiness is native.InputReadiness.READY
    assert plan.follow_on[0].after.state.selection is None


@pytest.mark.parametrize("failure", ["event", "multiple", "nested", "busy", "unrelated", "battle"])
def test_reward_plan_rejects_other_run_flows_and_unfinished_settlement(
    tmp_path: Path, failure: str
) -> None:
    archive, anchor = reward_source(tmp_path, skip=True, failure=failure)
    with pytest.raises(AdmissionFailure):
        plan_admission(
            AdmissionRequest(
                id="reward",
                archive=archive,
                anchor=anchor,
                endpoint=TargetBattle() if failure == "battle" else AfterDecisionsSpec(count=1),
            ),
            tmp_path,
        )


def reward_source(
    directory: Path, *, skip: bool, failure: str | None = None, initial_notes: str | None = None
) -> tuple[Path, ModelAnchor]:
    opening = run_state(1, "map")
    assert isinstance(opening.state, native.RunState)
    opening = opening.model_copy(
        update={
            "state": opening.state.model_copy(
                update={
                    "progress": opening.state.progress.model_copy(update={"room_type": "monster"}),
                    "player": opening.state.player.model_copy(
                        update={"definition_id": "CHARACTER.IRONCLAD"}
                    ),
                    "actions": (
                        native.Action(ref="interaction:1:1", label="Travel", enabled=True),
                    ),
                }
            )
        }
    )
    card = battle_state(1, "combat").objects["c1"].model_copy(update={"previews": ()})
    target = opening.model_copy(
        update={
            "location": opening.location.model_copy(update={"node_id": "reward"}),
            "input_readiness": native.InputReadiness.SELECTION,
            "objects": {
                **{f"card:2:{i}": card for i in (1, 2, 3)},
                "offer:2:4": native.OfferEntry(kind=native.ObjectKind.OFFER, name="Cards"),
                "option:2:10": native.OptionEntry(kind=native.ObjectKind.OPTION, name="Choice"),
            },
            "state": opening.state.model_copy(
                update={
                    "screen": native.RunScreen.EVENT
                    if failure == "event"
                    else native.RunScreen.REWARDS,
                    "deck": (),
                    "actions": (
                        *(
                            native.Action(
                                ref=f"interaction:2:{i + 4}",
                                label="Pick",
                                enabled=True,
                                subject=f"card:2:{i}",
                            )
                            for i in (1, 2, 3)
                        ),
                        native.Action(ref="interaction:2:8", label="Skip", enabled=True),
                        native.Action(
                            ref="interaction:2:9",
                            label="Proceed",
                            enabled=True,
                            operation=native.ActionOperation.PROCEED,
                        ),
                        native.Action(
                            ref="interaction:2:11", label="Claim", enabled=True, subject="offer:2:4"
                        ),
                    ),
                    "selection": native.Selection(
                        prompt="Choose",
                        source=None,
                        min=0,
                        max=2 if failure == "multiple" else 1,
                        candidates=("card:2:1", "card:2:2", "card:2:3"),
                        selected=(),
                    ),
                    "offers": (
                        native.Offer(
                            ref="offer:2:4",
                            kind=native.OfferKind.CARD,
                            contents=("card:2:1", "card:2:2", "card:2:3"),
                            actions=("interaction:2:11",),
                        ),
                    ),
                }
            ),
        }
    )
    assert isinstance(target.state, native.RunState)
    after = target.model_copy(
        update={
            "objects": {} if skip else {"card:3:1": card},
            "input_readiness": native.InputReadiness.BUSY
            if failure == "busy"
            else native.InputReadiness.READY,
            "state": target.state.model_copy(
                update={
                    "control": native.StateControl.BUSY
                    if failure == "busy"
                    else native.StateControl.READY,
                    "selection": target.state.selection if failure == "nested" else None,
                    "deck": () if skip else ("card:3:1",),
                    "offers": (),
                    "actions": (
                        native.Action(
                            ref="interaction:2:9",
                            label="Proceed",
                            enabled=True,
                            operation=native.ActionOperation.PROCEED,
                        ),
                    ),
                }
            ),
        }
    )
    archive = directory / "source.sqlite"
    manifest = Manifest(
        initial_notes=NotesState(other=initial_notes) if initial_notes is not None else None,
        materials=SessionMaterials(
            entry_documents_enabled=False, act_guidance={}, battle_guidance=()
        ),
        recording_id="source",
        core_version="test",
        game_instance_id="game",
        run_id="run",
        seed="SEED",
        template="test",
        headless=HeadlessInitialConditions(
            requested_character=StandardCharacter.IRONCLAD,
            requested_ascension=0,
            effective_settings_sha256="test",
            game_assembly_sha256="test",
            mod_version="test",
            mod_sha256="test",
            initial_observation_sha256="test",
            initial_control=native.ControlReply(mode=native.ControlIntent.RUNNING),
            start_receipt=NativeStartReceipt(
                status="completed", effect_verified=True, reason_code=None, seed="SEED"
            ),
        ),
    )
    with closing(Journal.create(archive, manifest)) as journal:
        journal.begin(Observe())
        journal.finish(GameReply(outcome=Observed(observation=opening), observations=(opening,)))
        journal.end()
        write_step(
            journal,
            opening,
            native.Interact(kind=native.StepKind.INTERACT, action="interaction:1:1"),
            "travel",
            target,
        )
        sequence = journal.begin_model(
            native.DecisionOwner.RUN,
            "run",
            (),
            ToolSet(tools=()),
            ConfigurationSnapshot.of(configuration(directory), CapacityPolicy()),
        )
        journal.finish_model(sequence)
        journal.end_model()
        step = (
            native.Interact(kind=native.StepKind.INTERACT, action="unrelated")
            if failure == "unrelated"
            else native.Select(kind=native.StepKind.SELECT, refs=() if skip else ("card:2:1",))
        )
        write_step(journal, target, step, "choice", after)
        write_step(
            journal,
            after,
            native.Interact(kind=native.StepKind.INTERACT, action="later"),
            "later",
            after,
        )
    return archive, ModelAnchor("source", sequence)
