import asyncio
import json
import sys
from contextlib import AbstractAsyncContextManager, closing
from enum import StrEnum
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import ValidationError
from test_assessment import write_step
from test_cases import admission_game
from test_combat import reply
from test_lifecycle import battle_state, run_state, terminal
from test_responses import configuration

from sts2_harness.capacity import CapacityPolicy
from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.config import Configuration, Driver
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.game import GameReply, Observe, Observed
from sts2_harness.headless import HeadlessOptions, StandardRunRecipe, run_headless
from sts2_harness.journal import (
    HeadlessInitialConditions,
    Journal,
    Manifest,
    NativeStartReceipt,
    SessionMaterials,
    StandardCharacter,
)
from sts2_harness.journal_models import ConfigurationSnapshot, ToolSet
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.session import RunSession
from sts2_harness_eval.cases import (
    AdmissionFailure,
    AdmissionRequest,
    Admitted,
    AfterDecisionsSpec,
    Case,
    CaseStore,
    Incomplete,
    IncompleteCause,
    Mismatch,
    Stage,
    TargetBattle,
    Unsupported,
    UnsupportedCause,
    admit_case,
    plan_admission,
)
from sts2_harness_eval.checkpoints import CheckpointClient
from sts2_harness_eval.native import NativeLaunch
from sts2_harness_eval.results import ModelAnchor
from sts2_harness_eval.verification import Relation


class RewardFailure(StrEnum):
    MISSING = "missing"
    DUPLICATE = "duplicate"
    PARENT = "parent"
    SLOT = "slot"
    FACTS = "facts"
    MALFORMED = "malformed"


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
        initial_notes=initial_notes,
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


def reward_game(directory: Path, *, skip: bool) -> tuple[Path, Path]:
    executable, template = admission_game(directory)
    fixture = directory / "fixture"
    fixture.mkdir()
    archive, _ = reward_source(fixture, skip=skip)
    with closing(Journal.open(archive)) as journal:
        states = [
            observation
            for record in journal.operations()
            if record.reply is not None
            for observation in record.reply.observations
        ][:3]
    states.append(terminal(states[-1]))
    script = executable.read_text()
    start = script.index("states = json.loads(")
    end = script.index("\n", start)
    script = (
        script[:start]
        + "states = json.loads("
        + repr(json.dumps([json.loads(state.to_json()) for state in states]))
        + ")"
        + script[end:]
    )
    script = script.replace(
        '("card", "enemy", "interaction")', '("card", "enemy", "interaction", "offer", "option")'
    )
    script = script.replace("if index == 1:", "if index == -99:")
    script = script.replace("index = 1\n", "index = 0\n")
    identity = """
            rewards = []
            if value["state"]["screen"] == "rewards":
                published = [*value["objects"],
                             *(item["ref"] for item in value["state"]["actions"])]
                for ref in published:
                    kind, _, ordinal = ref.split(":")
                    ordinal = int(ordinal) % 100
                    if kind == "card" and index != 1:
                        continue
                    binding_kind, slot, option = {
                        1: ("creation_card", 2, None),
                        2: ("creation_card", 0, None),
                        3: ("creation_card", 1, None),
                        4: ("reward_object", None, None),
                        5: ("pick_action", 2, None),
                        6: ("pick_action", 0, None),
                        7: ("pick_action", 1, None),
                        8: ("alternative_action", None, "SKIP"),
                        9: ("proceed_action", None, None),
                        10: ("choice_screen", None, None),
                        11: ("claim_action", None, None),
                    }[ordinal]
                    rewards.append({"ref": ref, "binding": {
                        "owner_net_id": 7,
                        "native_rewards_set_index": None if ordinal == 9 else 5,
                        "binding_kind": binding_kind,
                        "creation_slot": slot,
                        "native_option_id": option,
                    }})
                if menu and index == 1:
                    if (base.parent / "missing").exists():
                        rewards = [item for item in rewards
                                   if item["binding"]["binding_kind"] != "creation_card"]
                    if (base.parent / "duplicate").exists():
                        rewards.append(rewards[0])
                    if (base.parent / "parent").exists():
                        rewards[0]["binding"]["native_rewards_set_index"] = 1
                    if (base.parent / "slot").exists():
                        rewards[0]["binding"]["creation_slot"] = 99
                    if (base.parent / "malformed").exists():
                        rewards[0]["binding"]["creation_slot"] = None
                    if (base.parent / "facts").exists():
                        value["state"]["player"]["gold"] += 1
                cards = [] if index == 1 else [
                    {"ref": ref, "combat_card_id": None,
                     "deck_card": {"owner_net_id": 7, "deck_index": 12}}
                    for ref in value["objects"] if ref.startswith("card:")
                ]
"""
    script = script.replace("            creatures = [", identity + "            creatures = [")
    script = script.replace(
        '"map_travel": points,', '"map_travel": points, "reward_bindings": rewards,'
    )
    script = script.replace(
        '        event("action")',
        """        event("action")
        with (ledger.parent / "steps.jsonl").open("a") as output:
            output.write(json.dumps({"loaded": menu, "step": value["step"]}) + "\\n")""",
    )
    executable.write_text(script)
    return executable, template


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Owned fake executable uses the macOS launcher."
)
@pytest.mark.parametrize(
    ("skip", "failure"),
    [(False, None), (True, None), *((False, failure) for failure in RewardFailure)],
)
def test_reward_admission_translates_native_parent_and_creation_slot_before_publishing(
    tmp_path: Path, skip: bool, failure: RewardFailure | None
) -> None:
    executable, template = reward_game(tmp_path, skip=skip)
    archive, anchor = asyncio.run(record_reward_source(tmp_path, executable, template, skip=skip))

    class ClosedStore(CaseStore):
        def publish(self, case: Case) -> None:
            assert (tmp_path / "ledger").read_text().splitlines()[-1].startswith("closed:")
            assert not list(self.directory.glob(".admission-*"))
            super().publish(case)

    store = ClosedStore(tmp_path / "cases")
    original = archive.read_bytes()
    output = tmp_path / "native"
    output.mkdir()
    if failure is not None:
        (output / failure.value).touch()
    result = asyncio.run(
        admit_case(
            AdmissionRequest(
                id="reward", archive=archive, anchor=anchor, endpoint=AfterDecisionsSpec(count=1)
            ),
            NativeLaunch(executable, template, output),
            store,
        )
    )
    assert archive.read_bytes() == original
    assert (tmp_path / "ledger").read_text().splitlines()[-1].startswith("closed:")
    assert not list(store.directory.glob(".admission-*"))
    if failure is not None:
        assert not list((store.directory / "cases").glob("*.json"))
        if failure is RewardFailure.MALFORMED:
            assert result == Incomplete(stage=Stage.LOAD, cause=IncompleteCause.PROTOCOL)
        elif failure is RewardFailure.FACTS:
            assert isinstance(result, Mismatch), result
            assert result.relation is Relation.REPLAY_LOAD
            assert result.field_path == "state.player.gold"
        else:
            assert isinstance(result, Unsupported), result
            assert result.cause is UnsupportedCause.IDENTITY
        return
    assert result == Admitted(case_id="reward"), result
    case = store.load("reward")
    binding = store.binding(case)
    reference = store.reference(binding)
    assert len(reference.follow_on) == 1
    target = reference.target.read
    assert target.observation.location.node_id == "reward"
    card_key = next(
        item.native_key for item in binding.target.refs if item.source_ref == "card:2:1"
    )
    assert card_key.model_dump(mode="json") == {
        "kind": "reward",
        "binding": {
            "owner_net_id": 7,
            "native_rewards_set_index": 5,
            "binding_kind": "creation_card",
            "creation_slot": 2,
            "native_option_id": None,
        },
    }
    writes = [json.loads(line) for line in (tmp_path / "steps.jsonl").read_text().splitlines()]
    assert writes[3:] == [
        {"loaded": False, "step": {"kind": "interact", "action": "interaction:1:1"}},
        {"loaded": False, "step": {"kind": "select", "refs": [] if skip else ["card:2:1"]}},
        {"loaded": True, "step": {"kind": "interact", "action": "interaction:21:101"}},
        {"loaded": True, "step": {"kind": "select", "refs": [] if skip else ["card:22:101"]}},
    ]
    settled = reference.follow_on[0].read
    assert settled.observation.state.selection is None
    if skip:
        assert settled.identities.cards == ()
    else:
        assert settled.identities.cards[0].ref == "card:3:1"
        assert settled.identities.cards[0].deck_card is not None
        assert settled.identities.cards[0].deck_card.deck_index == 12
    assert all(item.ref != "card:3:1" for item in settled.identities.reward_bindings)
    assert archive.read_bytes() == original
    assert not list(store.directory.rglob("*.sqlite"))


async def record_reward_source(
    directory: Path, executable: Path, template: Path, *, skip: bool
) -> tuple[Path, ModelAnchor]:
    paths: list[Path] = []

    def factory(
        manifest: Manifest, archive: Path, diagnostics: Diagnostics
    ) -> AbstractAsyncContextManager[RunSession]:
        paths.append(archive)
        port = json.loads((archive.parent / "status.json").read_text())["mod_port"]
        responses = iter(
            [
                reply(
                    (
                        "travel",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"interaction:1:1"}]}',
                    )
                ),
                reply(
                    (
                        "choose",
                        "run_act",
                        json.dumps(
                            {"steps": [{"kind": "select", "refs": [] if skip else ["card:2:1"]}]}
                        ),
                    )
                ),
                reply(
                    (
                        "leave",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"interaction:2:9"}]}',
                    )
                ),
            ]
        )
        return live_session(
            ModClient(f"http://127.0.0.1:{port}"),
            manifest,
            archive,
            model=Llm(
                configuration(directory),
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                    ),
                ),
            ),
            diagnostics=diagnostics,
        )

    await run_headless(
        HeadlessOptions(
            executable,
            template,
            directory / "source",
            recipe=StandardRunRecipe(character=StandardCharacter.IRONCLAD, seed="SEED"),
        ),
        Configuration(Driver.BUILTIN, configuration(directory)),
        stop_requested=asyncio.Event(),
        session_factory=factory,
    )
    with closing(Journal.open(paths[0])) as journal:
        anchor = ModelAnchor(journal.manifest.recording_id, journal.requests()[1].sequence)
    return paths[0], anchor


@pytest.mark.parametrize(
    "binding",
    [
        {
            "binding_kind": "creation_card",
            "native_rewards_set_index": None,
            "creation_slot": 2,
            "native_option_id": None,
        },
        {
            "binding_kind": "proceed_action",
            "native_rewards_set_index": 5,
            "creation_slot": None,
            "native_option_id": None,
        },
        {
            "binding_kind": "reward_object",
            "native_rewards_set_index": 5,
            "creation_slot": 2,
            "native_option_id": None,
        },
        {
            "binding_kind": "alternative_action",
            "native_rewards_set_index": 5,
            "creation_slot": None,
            "native_option_id": None,
        },
    ],
)
def test_checkpoint_identity_rejects_contradictory_reward_addresses(
    binding: dict[str, str | int | None],
) -> None:
    payload = {
        "status": "completed",
        "observation": json.loads(run_state(1, "reward").to_json()),
        "identities": {
            "cards": [],
            "creatures": [],
            "map_travel": [],
            "relics": [],
            "reward_bindings": [
                {
                    "ref": "card:2:1",
                    "binding": {
                        "owner_net_id": 7,
                        **binding,
                    },
                }
            ],
        },
    }

    async def scenario() -> None:
        async with CheckpointClient(
            "http://fake",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=payload),
            ),
        ) as client:
            with pytest.raises(ValidationError):
                await client.identity()

    asyncio.run(scenario())
