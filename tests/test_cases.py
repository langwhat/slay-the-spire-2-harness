import asyncio
import json
import sqlite3
import sys
from contextlib import AbstractAsyncContextManager, closing
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_headless import game_fixture
from test_interactive import ready_run
from test_lifecycle import battle_state, terminal
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.config import Configuration, Driver
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.game import Settle, Settlement
from sts2_harness.headless import HeadlessOptions, StandardRunRecipe, run_headless
from sts2_harness.journal import (
    ArchiveError,
    DependencyKind,
    Journal,
    Manifest,
    ModelSummary,
    StandardCharacter,
)
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.session import RunSession
from sts2_harness_eval.cases import (
    AdmissionRequest,
    Admitted,
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
)
from sts2_harness_eval.native import NativeLaunch
from sts2_harness_eval.results import ModelAnchor
from sts2_harness_eval.verification import Relation


def admission_game(
    tmp_path: Path,
    *,
    combat_batch: bool = False,
    prefix_batch: bool = False,
    unpublished_card: bool = False,
) -> tuple[Path, Path]:
    executable, template = game_fixture(tmp_path)
    opening = json.loads(ready_run().to_json())
    opening["state"]["progress"]["room_type"] = "event"
    opening["state"]["player"]["definition_id"] = "CHARACTER.IRONCLAD"
    opening["location"]["node_id"] = "node-10"
    opening["state"]["actions"][0]["ref"] = "interaction:10:1"
    event = json.loads(json.dumps(opening))
    event["location"]["node_id"] = "node-9"
    event["state"]["screen"] = "event"
    event["state"]["actions"][0]["ref"] = "interaction:9:1"
    event["stamp"]["observation_sequence"] = 0
    if prefix_batch:
        another = dict(event["state"]["actions"][0])
        another["ref"] = "interaction:9:2"
        event["state"]["actions"].append(another)
    intermediate_event = json.loads(json.dumps(event))
    battle = json.loads(battle_state(1, "node-11").to_json())
    battle["state"]["player"]["definition_id"] = "CHARACTER.IRONCLAD"
    battle["input_readiness"] = "ready"
    battle["state"]["selection"] = None
    battle["state"]["screen"] = "combat"
    battle["state"]["turn"] = 1
    for field in ("upgrades", "generated", "modifier_descriptions"):
        battle[field] = {}
    battle["mechanisms"] = []
    encoded = json.dumps(battle)
    for old, new in (("c1", "card:11:1"), ("c2", "card:11:2"), ("e1", "enemy:11:3")):
        encoded = encoded.replace('"' + old + '"', '"' + new + '"')
    battle = json.loads(encoded)
    played = json.loads(encoded)
    played["state"]["piles"]["hand"] = ["card:11:2"]
    played["state"]["piles"]["discard"] = ["card:11:1"]
    second = json.loads(encoded)
    second["state"]["turn"] = 2
    second["stamp"]["observation_sequence"] = 3
    ended = json.loads(
        terminal(native.Observation.model_validate_json(json.dumps(second))).to_json()
    )
    probe = executable.read_text().split("initial = json.loads", 1)[0]
    runtime = r"""
import hashlib, signal

states = json.loads(STATES)
menu = "STS2AGENT_START_RUN_FILE" not in os.environ
index = -1 if menu else 0
mode = "paused"
publication_sequence = 0
pid = str(os.getpid())
base = pathlib.Path(os.environ["HOME"]).parent
ledger = pathlib.Path(LEDGER)


def event(name):
    with ledger.open("a") as output:
        output.write(name + ":" + pid + "\n")


event("menu" if menu else "recipe")


def finish(*args):
    event("closed")
    sys.exit(0)


signal.signal(signal.SIGTERM, finish)


def current():
    global publication_sequence
    publication_sequence += 1
    value = json.loads(json.dumps(states[index]))
    value["stamp"]["observation_sequence"] = publication_sequence
    value["location"]["game_instance_id"] = pid
    value["stamp"]["game_instance_id"] = pid
    value["location"]["run_id"] = "run-" + pid
    if menu:

        def replace(value):
            if isinstance(value, dict):
                return {replace(key): replace(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace(item) for item in value]
            if (
                isinstance(value, str)
                and len(value.split(":")) == 3
                and value.split(":")[0] in ("card", "enemy", "interaction")
            ):
                kind, epoch, ordinal = value.split(":")
                return kind + ":" + str(int(epoch) + 20) + ":" + str(int(ordinal) + 100)
            return value

        value = replace(value)
        value["location"]["node_id"] = "loaded-" + value["location"]["node_id"]
        if index == BOUNDARY_INDEX:
            value["state"]["progress"]["room_type"] = (
                "monster" if (base.parent / "map_mismatch").exists() else None
            )
        if (base.parent / "mismatch").exists() and index == BATTLE_INDEX + 1:
            value["state"]["player"]["hp"] -= 1
    if (base.parent / "r1").exists() and index == BOUNDARY_INDEX:
        value["state"]["player"]["gold"] += 1
    if menu and index == BATTLE_INDEX + 1:
        for field in ("game_instance_id", "run_id", "node_id"):
            if (base.parent / ("drift_" + field)).exists():
                value["location"][field] = "unexpected-other"
                if field == "game_instance_id":
                    value["stamp"][field] = "unexpected-other"
    return value


def proof(kind, path):
    value = current()
    return {
        "kind": kind,
        "file": {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        },
        "state": {
            "route": value["location"],
            "native_run_instance_id": 1,
            "native_revision": 1,
            "target": {
                "canonical_seed": "SEED",
                "game_mode": "standard",
                "character": "IRONCLAD",
                "ascension": 0,
                "act": 1,
                "floor": 0,
            },
        },
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, value):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())

    def do_GET(self):
        if self.path == "/status":
            location = (
                {"game_instance_id": pid, "run_id": None, "node_id": None, "owner": None}
                if index < 0
                else current()["location"]
            )
            return self.send(
                {
                    "location": location,
                    "control_intent": mode,
                    "input_readiness": "unavailable" if index < 0 else current()["input_readiness"],
                    "receipt": None,
                }
            )
        value = current()
        if self.path == "/operator/checkpoint/identity":
            assert os.environ["STS2_ALLOW_CHECKPOINTS"] == "true"
            if (base.parent / "prefix_identity_unavailable").exists() and index < BOUNDARY_INDEX:
                return self.send({
                    "status": "failed",
                    "error": {
                        "code": "execution_failed",
                        "message": "Native identities are unavailable before the checkpoint.",
                    },
                })
            cards = [
                {"ref": ref, "combat_card_id": int(ref.split(":")[2]) % 100, "deck_card": None}
                for ref in value["objects"]
                if ref.startswith("card:")
            ]
            if (base.parent / "identity").exists():
                cards = []
            creatures = [
                {"ref": item["ref"], "combat_id": 1} for item in value["state"].get("enemies", [])
            ]
            points = [
                {"ref": item["ref"], "act": 1, "row": 0, "col": 0}
                for item in value["state"]["actions"]
                if value["state"]["screen"] == "map"
            ]
            return self.send(
                {
                    "status": "completed",
                    "observation": value,
                    "identities": {
                        "cards": cards,
                        "creatures": creatures,
                        "relics": [],
                        "map_travel": points,
                    },
                }
            )
        return self.send({"status": "completed", "observation": value})

    def do_POST(self):
        global mode, index
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/control":
            mode = value["mode"]
            return self.send({"mode": mode})
        if self.path == "/operator/checkpoint":
            assert os.environ["STS2_ALLOW_CHECKPOINTS"] == "true" and mode == "paused"
            kind = value["kind"]
            event(kind)
            if kind == "capture":
                path = pathlib.Path(value["output_directory"])
                path.mkdir()
                path = path / "current_run.save"
                path.write_bytes(b"synthetic checkpoint")
            else:
                assert kind == "load" and index == -1
                path = pathlib.Path(value["checkpoint_path"])
                index = BOUNDARY_INDEX
            if (base.parent / "unknown").exists():
                return self.send(
                    {
                        "status": "unknown",
                        "command_id": value["command_id"],
                        "native_dispatched": True,
                        "error": {"code": "execution_uncertain", "message": "unknown"},
                    }
                )
            return self.send(
                {
                    "status": "completed",
                    "command_id": value["command_id"],
                    "proof": proof(kind, path),
                }
            )
        assert mode == "running"
        event("action")
        if COMBAT_BATCH and index == BATTLE_INDEX:
            observation = current()
            assert value["step"] == {
                "kind": "play_card",
                "card": (
                    "card:11:99" if UNPUBLISHED_CARD else observation["state"]["piles"]["hand"][-1]
                ),
                "target": observation["state"]["enemies"][0]["ref"],
            }
        if COMBAT_BATCH and index == BATTLE_INDEX + 1:
            assert value["step"] == {"kind": "end_turn"}
        index += 1
        boundary = "terminal" if index == len(states) - 1 else "ready"
        relation = "R2" if menu else "R1"
        if (base.parent / ("boundary_" + relation)).exists() and index == BATTLE_INDEX + 1:
            boundary = "selection"
        return self.send(
            {
                "operation_id": value["operation_id"],
                "status": "completed",
                "boundary": boundary,
            }
        )


server = http.server.HTTPServer(("127.0.0.1", int(os.environ["STS2AGENT_PORT"])), Handler)
if not menu:
    start = pathlib.Path(os.environ["STS2AGENT_START_RUN_FILE"])
    recipe = json.loads(start.read_text())
    pathlib.Path(str(start) + ".result.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "effect_verified": True,
                "reason_code": None,
                "seed": recipe["seed"],
            }
        )
    )
server.serve_forever()
"""
    states = [
        event,
        *([intermediate_event] if prefix_batch else []),
        opening,
        battle,
        *([played] if combat_batch else []),
        second,
        ended,
    ]
    runtime = runtime.replace("STATES", repr(json.dumps(states)))
    runtime = runtime.replace("COMBAT_BATCH", repr(combat_batch))
    runtime = runtime.replace("UNPUBLISHED_CARD", repr(unpublished_card))
    runtime = runtime.replace("BOUNDARY_INDEX", str(2 if prefix_batch else 1))
    runtime = runtime.replace("BATTLE_INDEX", str(3 if prefix_batch else 2))
    runtime = runtime.replace("LEDGER", repr(str(tmp_path / "ledger")))
    executable.write_text(probe + runtime)
    return executable, template


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Owned fake executable uses the macOS launcher."
)
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "mismatch",
        "unknown",
        "r1",
        "identity",
        "map_mismatch",
        "wrong_seed",
        "no_follow",
        "gap",
        "missing_template",
        "drift_game_instance_id",
        "drift_run_id",
        "drift_node_id",
    ],
)
def test_admission_replays_source_then_independently_loads_and_publishes_after_cleanup(
    tmp_path: Path,
    failure: str | None,
) -> None:
    executable, template = admission_game(tmp_path)
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
                        "event",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"interaction:9:1"}]}',
                    )
                ),
                reply(
                    (
                        "travel",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"interaction:10:1"}]}',
                    )
                ),
                reply(("turn", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("finish", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )
        return live_session(
            ModClient(f"http://127.0.0.1:{port}"),
            manifest,
            archive,
            model=Llm(
                configuration(tmp_path),
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                    )
                ),
            ),
            diagnostics=diagnostics,
        )

    async def scenario() -> None:
        await run_headless(
            HeadlessOptions(
                executable,
                template,
                tmp_path / "source",
                recipe=StandardRunRecipe(character=StandardCharacter.IRONCLAD, seed="SEED"),
            ),
            Configuration(Driver.BUILTIN, configuration(tmp_path)),
            stop_requested=asyncio.Event(),
            session_factory=factory,
        )
        with closing(Journal.open(paths[0])) as journal:
            target = next(
                record
                for record in journal.requests()
                if record.role is native.DecisionOwner.COMBAT
            )
            if failure == "no_follow":
                target = journal.requests()[-1]
            anchor = ModelAnchor(journal.manifest.recording_id, target.sequence)
            if failure == "wrong_seed":
                changed = journal.manifest.model_copy(update={"seed": "WRONG"})
                with sqlite3.connect(paths[0]) as connection:
                    connection.execute("UPDATE manifest SET value=?", (changed.model_dump_json(),))
            if failure == "gap":
                missing = next(
                    record for record in journal.operations() if record.sequence > target.sequence
                )
                with sqlite3.connect(paths[0]) as connection:
                    connection.execute("DELETE FROM records WHERE sequence=?", (missing.sequence,))
                    connection.execute(
                        "DELETE FROM summaries WHERE sequence=?", (missing.sequence,)
                    )
        output = tmp_path / "native"
        output.mkdir()
        (output / "prefix_identity_unavailable").touch()
        if failure is not None:
            (output / failure).touch()

        class CheckedStore(CaseStore):
            def publish(self, case: Case) -> None:
                assert (tmp_path / "ledger").read_text().splitlines()[-1].startswith("closed:")
                assert not list(self.directory.glob(".admission-*"))
                super().publish(case)

        store = CheckedStore(tmp_path / "cases")
        request = AdmissionRequest(
            id="one", archive=paths[0], anchor=anchor, endpoint=TargetBattle()
        )
        original = paths[0].read_bytes()
        ledger_before = (tmp_path / "ledger").read_bytes()
        launch_template = (
            tmp_path / "missing.manifest.json" if failure == "missing_template" else template
        )
        result = await admit_case(request, NativeLaunch(executable, launch_template, output), store)
        if failure in (
            "mismatch",
            "r1",
            "map_mismatch",
            "drift_game_instance_id",
            "drift_run_id",
            "drift_node_id",
        ):
            assert isinstance(result, Mismatch), result
        elif failure == "unknown":
            assert isinstance(result, Incomplete), result
        elif failure == "missing_template":
            assert result == Incomplete(stage=Stage.CAPTURE, cause=IncompleteCause.STARTUP)
            assert (tmp_path / "ledger").read_bytes() == ledger_before
        elif failure in ("identity", "wrong_seed", "no_follow", "gap"):
            assert isinstance(result, Unsupported), result
        else:
            assert result == Admitted(case_id="one"), result
            case = store.load("one")
            assert case.source.archive == paths[0]
            assert case.source.target_cursor == target.confirmed_operation_cursor
            assert len(store.binding(case).follow_on) == 1
            reference = store.reference(store.binding(case))
            assert reference.target.read.observation.state.phase is native.Phase.COMBAT
            assert reference.boundary.source_stamp == reference.suffix[0].source_stamp
            assert (
                reference.boundary.read.observation.stamp.observation_sequence
                < reference.suffix[0].read.observation.stamp.observation_sequence
            )
            # The same source boundary reuses its checkpoint; a fresh B still proves it.
            result = await admit_case(
                request.model_copy(update={"id": "two"}),
                NativeLaunch(executable, template, output),
                store,
            )
            assert result == Admitted(case_id="two"), result
            assert len(list((store.directory / "checkpoints").iterdir())) == 1
            frozen = (store.directory / "cases/one.json").read_bytes()
            duplicate = await admit_case(request, NativeLaunch(executable, template, output), store)
            assert isinstance(duplicate, Unsupported)
            assert (store.directory / "cases/one.json").read_bytes() == frozen
            store.checkpoint_path(case.checkpoint).write_bytes(b"replaced")
            with pytest.raises(ArchiveError):
                store.load("one")
        assert paths[0].read_bytes() == original
        assert not list(store.directory.rglob("*.sqlite"))
        assert not list(store.directory.glob(".admission-*"))
        ledger = (tmp_path / "ledger").read_text().splitlines()
        owners = [line for line in ledger if line.startswith(("recipe:", "menu:"))]
        for previous, following in zip(owners, owners[1:], strict=False):
            assert ledger.index("closed:" + previous.split(":")[1]) < ledger.index(following)
        assert ledger[-1].startswith("closed:")
        assert sum(line.startswith("capture:") for line in ledger) == (
            0 if failure in ("r1", "wrong_seed", "no_follow", "gap", "missing_template") else 1
        )
        assert bool(list((store.directory / "cases").glob("*.json"))) is (failure is None)

    asyncio.run(scenario())


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Owned fake executable uses the macOS launcher."
)
@pytest.mark.parametrize(
    ("prefix_batch", "failure", "unpublished_card"),
    [
        (False, None, False),
        (True, None, False),
        (False, Relation.SOURCE_REPLAY, False),
        (False, Relation.REPLAY_LOAD, False),
        (False, None, True),
    ],
)
def test_admission_restores_completed_combat_batch_with_one_publication(
    tmp_path: Path, prefix_batch: bool, failure: Relation | None, unpublished_card: bool
) -> None:
    executable, template = admission_game(
        tmp_path, combat_batch=True, prefix_batch=prefix_batch, unpublished_card=unpublished_card
    )
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
                        "event",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"interaction:9:1"}'
                        + (
                            ',{"kind":"interact","action":"interaction:9:2"}'
                            if prefix_batch
                            else ""
                        )
                        + "]}",
                    )
                ),
                reply(
                    (
                        "travel",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"interaction:10:1"}]}',
                    )
                ),
                reply(
                    (
                        "turn",
                        "combat_act",
                        '{"steps":[{"kind":"play_card","card":"'
                        + ("card:11:99" if unpublished_card else "card:11:1")
                        + '","target":"enemy:11:3"},{"kind":"end_turn"}]}',
                    )
                ),
                reply(("finish", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )
        return live_session(
            ModClient(f"http://127.0.0.1:{port}"),
            manifest,
            archive,
            model=Llm(
                configuration(tmp_path),
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                    )
                ),
            ),
            diagnostics=diagnostics,
        )

    async def scenario() -> None:
        await run_headless(
            HeadlessOptions(
                executable,
                template,
                tmp_path / "source",
                recipe=StandardRunRecipe(character=StandardCharacter.IRONCLAD, seed="SEED"),
            ),
            Configuration(Driver.BUILTIN, configuration(tmp_path)),
            stop_requested=asyncio.Event(),
            session_factory=factory,
        )
        with closing(Journal.open(paths[0])) as journal:
            target = next(
                summary
                for summary in journal.summaries(
                    kind=DependencyKind.MODEL, role=native.DecisionOwner.COMBAT
                )
                if isinstance(summary, ModelSummary)
            )
            anchor = ModelAnchor(journal.manifest.recording_id, target.sequence)
            operations = journal.operations(start=target.sequence)
            writes = [record for record in operations if isinstance(record.request, Settle)]
            play, end_turn = writes[:2]
            assert isinstance(play.request, Settle)
            assert play.request.step.kind is native.StepKind.PLAY_CARD
            assert isinstance(end_turn.request, Settle)
            assert end_turn.request.step.kind is native.StepKind.END_TURN
            for record in (play, end_turn):
                assert record.reply is not None
                assert isinstance(record.reply.outcome, Settlement)
                assert isinstance(record.reply.outcome.receipt, native.ReceiptCompleted)
            assert not any(
                record.reply is not None and record.reply.observations
                for record in operations
                if play.sequence <= record.sequence < end_turn.sequence
            )
            published = next(
                observation
                for record in operations
                if record.sequence >= end_turn.sequence and record.reply is not None
                for observation in record.reply.observations
            )
            assert isinstance(published.state, native.CombatState)
            assert published.state.turn == 2
        store = CaseStore(tmp_path / "cases")
        output = tmp_path / "native"
        output.mkdir()
        if failure is not None:
            (output / ("boundary_" + failure.value)).touch()
        result = await admit_case(
            AdmissionRequest(
                id="combat-batch", archive=paths[0], anchor=anchor, endpoint=TargetBattle()
            ),
            NativeLaunch(executable, template, output),
            store,
        )
        if unpublished_card:
            assert result == Unsupported(cause=UnsupportedCause.IDENTITY, sequence=play.sequence)
            assert not (store.directory / "cases/combat-batch.json").exists()
            return
        if failure is not None:
            assert result == Mismatch(
                relation=failure, sequence=play.sequence, field_path="receipt.boundary"
            )
            assert not (store.directory / "cases/combat-batch.json").exists()
            return
        assert result == Admitted(case_id="combat-batch"), result
        case = store.load("combat-batch")
        reference = store.reference(store.binding(case))
        assert isinstance(reference.target.read.observation.state, native.CombatState)
        assert reference.target.read.observation.state.turn == 1
        followed = reference.follow_on[-1].read.observation
        assert isinstance(followed.state, native.CombatState)
        assert followed.state.turn == 2
        ledger = (tmp_path / "ledger").read_text().splitlines()
        assert any(line.startswith("capture:") for line in ledger)
        assert any(line.startswith("load:") for line in ledger)
        assert ledger[-1].startswith("closed:")

    asyncio.run(scenario())


def test_admission_returns_incomplete_when_store_path_is_a_file(tmp_path: Path) -> None:
    occupied = tmp_path / "store"
    occupied.write_text("existing")
    request = AdmissionRequest(
        id="one",
        archive=tmp_path / "source.sqlite",
        anchor=ModelAnchor("source", 1),
        endpoint=TargetBattle(),
    )
    result = asyncio.run(
        admit_case(
            request,
            NativeLaunch(tmp_path / "game", tmp_path / "template", tmp_path / "out"),
            CaseStore(occupied),
        )
    )
    assert result == Incomplete(stage=Stage.PREPARATION, cause=IncompleteCause.IO)
    assert occupied.read_text() == "existing"
    assert tuple(tmp_path.iterdir()) == (occupied,)
