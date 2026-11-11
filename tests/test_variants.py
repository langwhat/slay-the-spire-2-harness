import asyncio
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_responses import configuration
from test_takeover import FakeRestorer, Source, source_case

from sts2_harness.client import models as native
from sts2_harness.context.models import ActContent
from sts2_harness.journal import Journal, SceneVariant
from sts2_harness.journal_models import (
    AssistantSnapshot,
    NamedText,
    PartsText,
    ResultSnapshot,
    SkillsData,
    TaskSnapshot,
    TextSnapshot,
    VariantPrefixOutcome,
)
from sts2_harness.llm.client import Llm
from sts2_harness.materials import DocumentSource, FileDocuments
from sts2_harness.prompts import PackagedPrompts
from sts2_harness_eval import ModelAnchor, Reached, Rejected, reach, take_over
from sts2_harness_eval.results import InputComponent, InputDifference
from sts2_harness_eval.sample import SampleBudget
from sts2_harness_eval.takeover import TakeoverCompleted
from sts2_harness_eval.variants import (
    HistoricalStart,
    MaterialReplacement,
    MaterialVariant,
    SceneStart,
)


def test_historical_variant_retains_paged_discovery_and_native_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = Source()
        source.responses = iter(
            [
                next(source.responses),
                reply(
                    (
                        "discover",
                        "combat_skill",
                        '{"mode":"list","text":"queen","offset":1,"limit":1}',
                    )
                ),
                *source.responses,
            ]
        )
        documents = FileDocuments(
            tuple(
                DocumentSource(f"guide:{i}", f"Guide {i}", "Queen response", tmp_path / "unused")
                for i in range(3)
            )
        )
        case = await source_case(
            tmp_path / "source.sqlite",
            start=False,
            source=source,
            documents=documents,
            target_position=3,
        )
        with closing(Journal.open(case.source.archive)) as original:
            recorded = original.request(case.source.anchor.model_sequence)
            discovery = next(
                item
                for item in recorded.input
                if isinstance(item, ResultSnapshot) and isinstance(item.data, SkillsData)
            )
            assert isinstance(discovery.data, SkillsData)
            assert [skill.id for skill in discovery.data.skills] == ["guide:1"]
            assert discovery.data.total == 3 and discovery.data.next_offset == 2
        prompt = PackagedPrompts().read("combat-task-rules")
        variant = MaterialVariant(
            start=HistoricalStart(),
            replacements=(
                MaterialReplacement(prompt.id, frozenset({prompt.version}), "Act carefully."),
            ),
        )
        original_bytes = case.source.archive.read_bytes()
        restorer = FakeRestorer(tmp_path, case)
        responses = iter(
            [
                reply(("live-play", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("live-turn", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )
        branch = tmp_path / "candidate.sqlite"
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                )
            ),
        ) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(3, 30),
                variant=variant,
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverCompleted)
        with closing(Journal.open(branch)) as journal:
            assert result.recording_id == journal.manifest.recording_id
            assert journal.manifest.variant is not None
            saved = journal.manifest.variant.replacements[0].replacement
            assert (saved.id, saved.body) == (prompt.id, "Act carefully.")
            preparations = journal.variant_preparations()
            assert preparations[-1].outcome is VariantPrefixOutcome.ANCHOR_READY
            assert preparations[-1].source_model_sequence == case.source.anchor.model_sequence
            assert all(item.source_reply_sequence is not None for item in preparations[:-1])
            assert all(item.configuration is not None for item in preparations)
            resumed = journal.request(case.source.anchor.model_sequence)
            replayed_discovery = next(
                item
                for item in resumed.input
                if isinstance(item, ResultSnapshot) and isinstance(item.data, SkillsData)
            )
            assert replayed_discovery.data == discovery.data
            target = journal.requests()[-1]
            anchor = ModelAnchor(journal.manifest.recording_id, target.sequence)
        async with reach(branch, anchor) as reached:
            assert isinstance(reached, Reached)
            assert reached.semantic_digest == target.semantic_digest
        assert case.source.archive.read_bytes() == original_bytes

    asyncio.run(scenario())


@pytest.mark.parametrize("candidate", [False, True])
def test_scene_starts_fresh_and_reopens_actual_requests(tmp_path: Path, candidate: bool) -> None:
    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
        original = case.source.archive.read_bytes()
        prompt = PackagedPrompts().read("combat-task-rules")
        declared = (
            (MaterialReplacement(prompt.id, frozenset({prompt.version}), "Act carefully."),)
            if candidate
            else ()
        )
        restorer = FakeRestorer(tmp_path, case)
        branch = tmp_path / "scene.sqlite"
        responses = iter(
            [
                reply(("fresh-first", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("fresh-second", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                )
            ),
        ) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(3, 30),
                variant=MaterialVariant(SceneStart("Fresh plan."), declared),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverCompleted)
        with closing(Journal.open(branch)) as journal:
            assert result.recording_id == journal.manifest.recording_id
            assert journal.manifest.parent is None
            assert journal.manifest.headless is None
            assert journal.manifest.game_instance_id == "B"
            assert journal.manifest.initial_notes == "Fresh plan."
            assert journal.manifest.template == "fresh-template"
            assert journal.manifest.game_version == "fresh-version"
            assert isinstance(journal.manifest.variant, SceneVariant)
            assert journal.manifest.variant.case_id == case.id
            assert (
                journal.manifest.variant.case_sha256
                == sha256(case.model_dump_json().encode()).hexdigest()
            )
            assert journal.manifest.variant.source.recording_id == "source"
            assert journal.manifest.variant.initial.restore.native_run_instance_id == 99
            assert journal.manifest.variant.initial.observation.location.game_instance_id == "B"
            requests = journal.requests()
            first = requests[0]
            assert first.role is native.DecisionOwner.COMBAT
            assert first.position == 1
            assert not any(
                isinstance(item, (AssistantSnapshot, ResultSnapshot)) for item in first.input
            )
            system = first.input[0]
            assert isinstance(system, TextSnapshot)
            assert isinstance(system.value, PartsText)
            rules = next(
                part.material
                for part in system.value.parts
                if isinstance(part, NamedText) and part.material.id == prompt.id
            )
            assert rules.body == ("Act carefully." if candidate else prompt.body)
            task = next(item for item in first.input if isinstance(item, TaskSnapshot))
            assert isinstance(task.content, ActContent)
            assert task.content.opening_notes == "Fresh plan."
            assert result.anchor.anchor == ModelAnchor(
                journal.manifest.recording_id, first.sequence
            )
            anchors = [
                ModelAnchor(journal.manifest.recording_id, item.sequence) for item in requests
            ]
        for anchor in anchors:
            async with reach(branch, anchor) as reached:
                assert isinstance(reached, Reached)
        assert case.source.archive.read_bytes() == original
        if candidate:
            import json
            import sqlite3

            with sqlite3.connect(branch) as connection:
                saved = json.loads(connection.execute("SELECT value FROM manifest").fetchone()[0])
                saved["variant"]["initial"]["observation"]["state"]["turn"] = 99
                connection.execute("UPDATE manifest SET value = ?", (json.dumps(saved),))
            async with reach(branch, anchors[0]) as rejected:
                assert isinstance(rejected, Rejected)
                assert rejected.detail == InputDifference(InputComponent.REQUEST)

    asyncio.run(scenario())


def test_scene_continues_through_normal_combat_review_and_run_handoff(tmp_path: Path) -> None:
    from test_lifecycle import run_state

    from sts2_harness.context.models import RunActHandoffContent
    from sts2_harness.journal_models import RunTaskSnapshot

    class ExitRestorer(FakeRestorer):
        def respond(self, request: httpx.Request) -> httpx.Response:
            response = super().respond(request)
            if request.url.path == "/action" and len(self.actions) == 1:
                reward = run_state(1, "reward")
                self.observation = reward.model_copy(
                    update={
                        "location": reward.location.model_copy(
                            update={"game_instance_id": "B", "run_id": "B-run"}
                        ),
                        "stamp": reward.stamp.model_copy(update={"game_instance_id": "B"}),
                    }
                )
            return response

    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
        restorer = ExitRestorer(tmp_path, case)
        responses = iter(
            [
                reply(("exit", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(text="Preserve resources after this battle."),
                reply(("reward", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
            ]
        )
        branch = tmp_path / "handoff.sqlite"
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                )
            ),
        ) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(4, 30),
                variant=MaterialVariant(SceneStart(""), ()),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverCompleted)
        with closing(Journal.open(branch)) as journal:
            assert result.recording_id == journal.manifest.recording_id
            requests = journal.requests()
            assert [item.role for item in requests] == [
                native.DecisionOwner.COMBAT,
                native.DecisionOwner.COMBAT,
                native.DecisionOwner.RUN,
            ]
            task = next(item for item in requests[-1].input if isinstance(item, RunTaskSnapshot))
            assert isinstance(task.content, RunActHandoffContent)
            assert task.content.strategy_notes == ""
            assert task.content.combat_review == "Preserve resources after this battle."
            assert [action.expected_route.owner for action in restorer.actions] == [
                native.DecisionOwner.COMBAT,
                native.DecisionOwner.RUN,
            ]
            anchor = ModelAnchor(journal.manifest.recording_id, requests[-1].sequence)
        async with reach(branch, anchor) as reached:
            assert isinstance(reached, Reached)

    asyncio.run(scenario())
