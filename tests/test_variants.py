import asyncio
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING
from test_combat import reply
from test_responses import configuration
from test_takeover import FakeRestorer, Source, source_case

from sts2_harness.client import models as native
from sts2_harness.context.models import ActContent, RunObservation
from sts2_harness.game import GameReply, GameSuccess
from sts2_harness.journal import Journal, ParentPrefix, SceneVariant, SessionMaterials
from sts2_harness.journal_models import (
    AssistantSnapshot,
    DocumentData,
    GuidanceText,
    NamedText,
    PartsText,
    ResultSnapshot,
    TaskSnapshot,
    TextSnapshot,
    ToolSet,
    VariantPrefixOutcome,
)
from sts2_harness.knowledge import Document, DocumentView, SkillRead, SkillSummary, SubjectId
from sts2_harness.llm.client import Llm
from sts2_harness.material_text import MaterialText
from sts2_harness.materials import FileDocuments
from sts2_harness.prompts import PackagedPrompts
from sts2_harness_eval import ModelAnchor, Reached, Rejected, reach, take_over
from sts2_harness_eval.cases import (
    AdmissionRequest,
    AfterDecisionsSpec,
    Unsupported,
    UnsupportedCause,
    plan_admission,
)
from sts2_harness_eval.results import InputComponent, InputDifference, MaterialDifference
from sts2_harness_eval.sample import SampleBudget
from sts2_harness_eval.takeover import TakeoverCompleted, TakeoverUnsupported
from sts2_harness_eval.variants import (
    HistoricalStart,
    MaterialReplacement,
    MaterialVariant,
    SceneStart,
)


def test_historical_variant_retains_native_history_and_reopens(tmp_path: Path) -> None:
    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
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
            target = journal.requests()[-1]
            anchor = ModelAnchor(journal.manifest.recording_id, target.sequence)
        async with reach(branch, anchor) as reached:
            assert isinstance(reached, Reached)
            assert reached.semantic_digest == target.semantic_digest
        assert case.source.archive.read_bytes() == original_bytes

    asyncio.run(scenario())


class VersionDocuments:
    def __init__(self, versions: tuple[str, ...]) -> None:
        self._versions = iter(versions)

    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        return ()

    async def read(self, request: SkillRead) -> DocumentView:
        return DocumentView(
            id="guide",
            name=MaterialText.create("guide:name", "Guide"),
            text=MaterialText.create("guide:text", next(self._versions)),
            resources=(),
        )

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        return ()


def document_source() -> Source:
    source = Source()
    source.responses = iter(
        [
            reply(("travel", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
            reply(("read-v1", "combat_skill", '{"mode":"read","id":"guide"}')),
            reply(("read-v2", "combat_skill", '{"mode":"read","id":"guide"}')),
            reply(("reread-v2", "combat_skill", '{"mode":"read","id":"guide"}')),
            reply(("play", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(("turn", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(("finish", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
        ]
    )
    return source


@pytest.mark.parametrize("mode", ["complete", "missing-version", "live-version"])
def test_document_versions_keep_explicit_result_links_and_reject_unknown_sources(
    tmp_path: Path, mode: str
) -> None:
    async def scenario() -> None:
        case = await source_case(
            tmp_path / "source.sqlite",
            start=False,
            source=document_source(),
            documents=VersionDocuments(("First", "Second", "Second")),
            target_position=4,
        )
        versions = frozenset(
            MaterialText.create("guide:text", body).version
            for body in (("First",) if mode == "missing-version" else ("First", "Second"))
        )
        with closing(Journal.open(case.source.archive)) as source:
            original = source.request(case.source.anchor.model_sequence)
            earlier = next(
                item
                for item in source.requests()
                if item.role is native.DecisionOwner.COMBAT and item.position == 3
            )
            tool = next(tool for tool in original.tools if tool.name == "combat_skill")
        variant = MaterialVariant(
            HistoricalStart(),
            (
                MaterialReplacement("guide:text", versions, "Plan from the current public state."),
                MaterialReplacement(
                    tool.description.id,
                    frozenset({tool.description.version}),
                    "Read supplementary guidance.",
                ),
            ),
        )
        original_bytes = case.source.archive.read_bytes()
        restorer = FakeRestorer(tmp_path, case)
        responses = iter(
            [
                reply(("future-read", "combat_skill", '{"mode":"read","id":"guide"}'))
                if mode == "live-version"
                else reply(("live-turn", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("last-turn", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
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
                VersionDocuments(("Third",)),
                SampleBudget(4, 30),
                variant=variant,
                stop_requested=lambda: False,
            )
        if mode != "complete":
            assert isinstance(result, TakeoverUnsupported)
            assert isinstance(result.evidence, Rejected)
            assert isinstance(result.evidence.detail, MaterialDifference)
            assert restorer.launches == (0 if mode == "missing-version" else 1)
            assert not restorer.actions
            return
        assert isinstance(result, TakeoverCompleted)
        with closing(Journal.open(branch)) as journal:
            assert result.recording_id == journal.manifest.recording_id
            resumed = journal.request(original.sequence)
            original_results = [item for item in original.input if isinstance(item, ResultSnapshot)]
            actual_results = [item for item in resumed.input if isinstance(item, ResultSnapshot)]
            assert [item.call_id for item in actual_results] == [
                item.call_id for item in original_results
            ]
            assert len(actual_results) == 3
            for item in actual_results:
                assert isinstance(item.data, DocumentData)
                assert item.data.document.text.body == "Plan from the current public state."
                assert "Plan from the current public state." in item.text
            assert [item for item in resumed.input if isinstance(item, AssistantSnapshot)] == [
                item for item in original.input if isinstance(item, AssistantSnapshot)
            ]
            selected = next(tool for tool in resumed.tools if tool.name == "combat_skill")
            assert selected.description.body == "Read supplementary guidance."
            assert selected.parameters == tool.parameters
            target = journal.requests()[-1]
            anchor = ModelAnchor(journal.manifest.recording_id, target.sequence)
        async with reach(branch, anchor) as reached:
            assert isinstance(reached, Reached)
        assert case.source.archive.read_bytes() == original_bytes
        with closing(Journal.open(branch)) as journal:
            assert result.recording_id == journal.manifest.recording_id
            manifest = journal.manifest.model_copy(
                update={
                    "recording_id": "broken-link",
                    "parent": ParentPrefix(
                        archive=branch.resolve(),
                        recording_id=journal.manifest.recording_id,
                        before_sequence=resumed.sequence,
                        archive_sha256=sha256(branch.read_bytes()).hexdigest(),
                    ),
                }
            )
        broken = tmp_path / "broken.sqlite"
        with closing(Journal.create(broken, manifest)) as journal:
            changed = tuple(
                item.model_copy(update={"call_id": "wrong-read"})
                if isinstance(item, ResultSnapshot) and item.call_id == "read-v1"
                else item
                for item in resumed.input
            )
            sequence = journal.begin_model(
                resumed.role,
                resumed.stream,
                changed,
                ToolSet(tools=resumed.tools),
                resumed.configuration,
            )
            journal.end_model()
        async with reach(broken, ModelAnchor("broken-link", sequence)) as reached:
            assert isinstance(reached, Rejected)
            assert reached.reason.value == "unsupported_history"

        assert manifest.parent is not None
        candidate_plan = plan_admission(
            AdmissionRequest(
                id="continued-case",
                archive=branch,
                anchor=ModelAnchor(manifest.parent.recording_id, earlier.sequence),
                endpoint=case.endpoint,
            ),
            tmp_path,
        )
        continued_case = case.model_copy(
            update={"id": "continued-case", "source": candidate_plan.source}
        )
        continued_responses = iter(
            [
                reply(("continued-one", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("continued-two", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )
        continuation = tmp_path / "continuation.sqlite"
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=next(continued_responses),
                    headers={"Content-Type": "text/event-stream"},
                )
            ),
        ) as model:
            result = await take_over(
                continued_case,
                FakeRestorer(tmp_path, continued_case),
                continuation,
                model,
                PackagedPrompts(),
                VersionDocuments(("Second",)),
                SampleBudget(3, 30),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverCompleted)
        with closing(Journal.open(continuation)) as journal:
            last = journal.requests()[-1]
            continued_anchor = ModelAnchor(journal.manifest.recording_id, last.sequence)
            assert journal.manifest.variant == manifest.variant
        async with reach(continuation, continued_anchor) as reached:
            assert isinstance(reached, Reached)
        assert earlier.sequence < original.sequence
        assert case.source.archive.read_bytes() == original_bytes
        changed_variant = MaterialVariant(
            HistoricalStart(),
            (MaterialReplacement("guide:text", versions, "Use a different plan."),),
        )
        rejected_restorer = FakeRestorer(tmp_path, continued_case)
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(
                lambda _: pytest.fail("A different nested variant must not call a provider")
            ),
        ) as model:
            refusal = await take_over(
                continued_case,
                rejected_restorer,
                tmp_path / "different.sqlite",
                model,
                PackagedPrompts(),
                VersionDocuments(("Second",)),
                SampleBudget(3, 30),
                variant=changed_variant,
                stop_requested=lambda: False,
            )
        assert isinstance(refusal, TakeoverUnsupported)
        assert isinstance(refusal.evidence, Rejected)
        assert isinstance(refusal.evidence.detail, MaterialDifference)
        assert rejected_restorer.launches == 0

    asyncio.run(scenario())


class AutomaticDocuments:
    def __init__(self, *, mixed: bool) -> None:
        self._reads = 0
        self._mixed = mixed

    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        return ()

    async def read(self, request: SkillRead) -> DocumentView:
        if request.id == "guide":
            self._reads += 1
            return DocumentView(
                id="guide",
                name=MaterialText.create("guide:name", "Guide"),
                text=MaterialText.create("guide:text", "First" if self._reads == 1 else "Second"),
                resources=(),
            )
        return DocumentView(
            id="other",
            name=MaterialText.create("other:name", "Other guide"),
            text=MaterialText.create(
                "other:text", "Changed" if self._mixed and self._reads > 1 else "Unchanged"
            ),
            resources=(),
        )

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        return ()


@pytest.mark.parametrize("mixed", [False, True])
def test_automatic_material_versions_deduplicate_only_complete_delivered_views(
    tmp_path: Path, mixed: bool
) -> None:
    async def scenario() -> None:
        case = await source_case(
            tmp_path / "source.sqlite",
            start=False,
            documents=AutomaticDocuments(mixed=mixed),
            materials=SessionMaterials(
                entry_documents_enabled=True, act_guidance={}, battle_guidance=("guide", "other")
            ),
        )
        variant = MaterialVariant(
            HistoricalStart(),
            (
                MaterialReplacement(
                    "guide:text",
                    frozenset(
                        MaterialText.create("guide:text", body).version
                        for body in ("First", "Second")
                    ),
                    "Second",
                ),
            ),
        )
        responses = iter(
            [
                reply(("live-one", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("live-two", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )
        branch = tmp_path / "automatic.sqlite"
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
                FakeRestorer(tmp_path, case),
                branch,
                model,
                PackagedPrompts(),
                AutomaticDocuments(mixed=mixed),
                SampleBudget(3, 30),
                variant=variant,
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverCompleted)
        with closing(Journal.open(branch)) as journal:
            assert result.recording_id == journal.manifest.recording_id
            assert journal.manifest.schema_version == 13
            assert journal.manifest.materials.battle_guidance == ("guide", "other")
            selected = journal.request(case.source.anchor.model_sequence)
            guidance = [
                message.value
                for message in selected.input
                if isinstance(message, TextSnapshot) and isinstance(message.value, GuidanceText)
            ]
            assert [[doc.id for doc in message.documents] for message in guidance] == (
                [["guide", "other"], ["other"]] if mixed else [["guide", "other"]]
            )
            assert guidance[0].documents[0].text.body == "Second"
            if mixed:
                assert guidance[1].documents[0].text.body == "Changed"
            anchor = ModelAnchor(journal.manifest.recording_id, journal.requests()[-1].sequence)
        async with reach(branch, anchor) as reached:
            assert isinstance(reached, Reached)

    asyncio.run(scenario())


class ActionBoundarySource(Source):
    def __init__(self, *, observation_changed: bool) -> None:
        super().__init__()
        self._observation_changed = observation_changed
        self._boundary_reads = 0
        self.responses = iter(
            [
                reply(("travel", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
                reply(("first", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("second", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("third", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state" and self.index == 2:
            self._boundary_reads += 1
            if self._observation_changed and self._boundary_reads == 2:
                # The incoming group snapshot must omit this retired description.
                self.states[2] = self.current.model_copy(
                    update={
                        "descriptions": {
                            key: value
                            for key, value in self.current.descriptions.items()
                            if key != "card:1:2"
                        }
                    }
                )
        return super().mod(request)


@pytest.mark.parametrize("difference", ["ordinary", "observation", "notes"])
def test_action_boundary_tasks_retain_observations_and_validate_recorded_notes(
    tmp_path: Path, difference: str
) -> None:
    async def scenario() -> None:
        case = await source_case(
            tmp_path / "source.sqlite",
            start=False,
            source=ActionBoundarySource(observation_changed=difference == "observation"),
            documents=AutomaticDocuments(mixed=False),
            materials=SessionMaterials(
                entry_documents_enabled=True, act_guidance={}, battle_guidance=("guide", "other")
            ),
        )
        with closing(Journal.open(case.source.archive)) as journal:
            original = journal.request(case.source.anchor.model_sequence)
            follow_on = journal.operations(start=original.sequence)[:2]
            manifest = journal.manifest.model_copy(
                update={
                    "recording_id": "extra-notes",
                    "parent": ParentPrefix(
                        archive=case.source.archive.resolve(),
                        recording_id=journal.manifest.recording_id,
                        before_sequence=original.sequence,
                        archive_sha256=sha256(case.source.archive.read_bytes()).hexdigest(),
                    ),
                }
            )
        tasks = [item for item in original.input if isinstance(item, TaskSnapshot)]
        assert len(tasks) == 2
        assert isinstance(tasks[-1].content, ActContent)
        action_result = next(item for item in original.input if isinstance(item, ResultSnapshot))
        assert action_result.observation is None
        assert isinstance(tasks[0].content, ActContent)
        initial = tasks[0].content.observation
        if difference == "observation":
            incoming = tasks[-1].content.observation
            assert initial is not None and incoming is not None
            assert initial.scene is not MISSING and initial.current is not MISSING
            assert incoming.scene is MISSING
            assert incoming.decision == initial.decision
            assert initial.current.card_descriptions is not MISSING
            assert incoming.current is not MISSING
            assert incoming.current.model_dump(mode="json") == {
                "removed": {"card_descriptions": ["card:1:2"]}
            }
        else:
            assert tasks[-1].content.observation == RunObservation(
                decision=initial.decision,
            )
        if difference == "notes":
            invalid = tmp_path / "extra-notes.sqlite"
            extra_notes = tasks[-1].model_copy(
                update={
                    "content": tasks[-1].content.model_copy(update={"opening_notes": "New notes"})
                }
            )
            changed = tuple(extra_notes if item is tasks[-1] else item for item in original.input)
            with closing(Journal.create(invalid, manifest)) as journal:
                sequence = journal.begin_model(
                    original.role,
                    original.stream,
                    changed,
                    ToolSet(tools=original.tools),
                    original.configuration,
                )
                journal.end_model()
                for record in follow_on:
                    assert record.reply is not None
                    journal.begin(record.request)
                    if record.operation_id is not None:
                        journal.submitting(record.operation_id)
                    for attempt in record.attempts:
                        journal.received(attempt)
                    journal.finish(
                        GameReply[GameSuccess](
                            outcome=record.reply.outcome, observations=record.reply.observations
                        )
                    )
                    journal.end()
            plan = plan_admission(
                AdmissionRequest(
                    id="extra-notes",
                    archive=invalid,
                    anchor=ModelAnchor("extra-notes", sequence),
                    endpoint=case.endpoint,
                ),
                tmp_path,
            )
            case = case.model_copy(update={"id": "extra-notes", "source": plan.source})
        original_bytes = case.source.archive.read_bytes()
        variant = MaterialVariant(
            HistoricalStart(),
            (
                MaterialReplacement(
                    "guide:text",
                    frozenset(
                        MaterialText.create("guide:text", body).version
                        for body in ("First", "Second")
                    ),
                    "Second",
                ),
            ),
        )
        restorer = FakeRestorer(tmp_path, case)
        responses = iter(
            [
                reply(("live-one", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("live-two", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )
        branch = tmp_path / "action-boundary.sqlite"
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
                AutomaticDocuments(mixed=False),
                SampleBudget(3, 30),
                stop_requested=lambda: False,
                variant=variant,
            )
        assert case.source.archive.read_bytes() == original_bytes
        if difference == "notes":
            assert isinstance(result, TakeoverUnsupported)
            assert isinstance(result.evidence, Rejected)
            assert result.evidence.detail == InputDifference(InputComponent.MODEL_INPUT)
            assert restorer.launches == 0
            return
        assert isinstance(result, TakeoverCompleted)
        assert restorer.launches == 1
        assert len(restorer.actions) == 2
        with closing(Journal.open(branch)) as journal:
            assert result.recording_id == journal.manifest.recording_id
            candidate = journal.request(original.sequence)
            retained_tasks = [item for item in candidate.input if isinstance(item, TaskSnapshot)]
            assert len(retained_tasks) == 2
            assert retained_tasks[-1] == tasks[-1]
            assert [item for item in candidate.input if isinstance(item, AssistantSnapshot)] == [
                item for item in original.input if isinstance(item, AssistantSnapshot)
            ]
            assert [item for item in candidate.input if isinstance(item, ResultSnapshot)] == [
                item for item in original.input if isinstance(item, ResultSnapshot)
            ]
            last = journal.requests()[-1]
            anchor = ModelAnchor(journal.manifest.recording_id, last.sequence)
        async with reach(branch, anchor) as reached:
            assert isinstance(reached, Reached)

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
            projection = task.content.observation
            assert projection.scene is not MISSING
            assert {item.ref for item in projection.scene.bindings} == {"card:9:1", "card:9:2"}
            assert projection.current is not MISSING
            assert projection.current.card_descriptions is not MISSING
            assert {item.ref: item.text for item in projection.current.card_descriptions} == {
                "card:9:1": None,
                "card:9:2": "造成3点伤害。",
            }
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


def test_scene_rejects_run_rewards_before_loading_or_starting(tmp_path: Path) -> None:
    from test_reward_admission import reward_source

    async def scenario() -> None:
        original = await source_case(tmp_path / "combat.sqlite", start=False)
        archive, anchor = reward_source(tmp_path, skip=True, initial_notes="")
        endpoint = AfterDecisionsSpec(count=1)
        plan = plan_admission(
            AdmissionRequest(id="reward", archive=archive, anchor=anchor, endpoint=endpoint),
            tmp_path,
        )
        case = original.model_copy(update={"source": plan.source, "endpoint": endpoint})
        restorer = FakeRestorer(tmp_path, original, uncertain=True)
        branch = tmp_path / "scene.sqlite"
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(lambda _: pytest.fail("No Scene model request.")),
        ) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(3, 30),
                variant=MaterialVariant(SceneStart(""), ()),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverUnsupported)
        assert isinstance(result.evidence, Unsupported)
        assert result.evidence.cause is UnsupportedCause.TARGET
        assert result.recording_id is None
        assert not branch.exists()
        assert restorer.launches == 0
        assert not restorer.started and not restorer.actions

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


def test_scene_without_a_model_request_has_no_anchor(tmp_path: Path) -> None:
    from sts2_harness_eval.takeover import TakeoverIncomplete

    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
        restorer = FakeRestorer(tmp_path, case)
        branch = tmp_path / "no-request.sqlite"
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(
                lambda _: pytest.fail("No provider request is allowed by the budget.")
            ),
        ) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(0, 30),
                variant=MaterialVariant(SceneStart(""), ()),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverIncomplete)
        assert result.anchor is None
        with closing(Journal.open(branch)) as journal:
            assert result.recording_id == journal.manifest.recording_id
            assert journal.requests() == ()
        assert not restorer.actions

    asyncio.run(scenario())


@pytest.mark.parametrize("automatic", [False, True])
def test_scene_rejects_unknown_live_material_before_actor_failure_or_pause(
    tmp_path: Path, automatic: bool
) -> None:
    from sts2_harness.game import Pause, Settle

    async def scenario() -> None:
        case = await source_case(
            tmp_path / "source.sqlite",
            start=False,
            source=document_source(),
            documents=VersionDocuments(("First",) + ("Second",) * 10),
            target_position=4,
            materials=SessionMaterials(
                entry_documents_enabled=automatic,
                act_guidance={},
                battle_guidance=("guide",) if automatic else (),
            ),
        )
        restorer = FakeRestorer(tmp_path, case)
        responses = iter(
            [
                reply(("unknown-read", "combat_skill", '{"mode":"read","id":"guide"}')),
                reply(("continue-one", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("continue-two", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )
        branch = tmp_path / "rejected-scene.sqlite"
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
                VersionDocuments(("Third",) * 10),
                SampleBudget(4, 30),
                variant=MaterialVariant(
                    SceneStart(""),
                    (
                        MaterialReplacement(
                            "guide:text",
                            frozenset(
                                MaterialText.create("guide:text", body).version
                                for body in ("First", "Second")
                            ),
                            "Declared replacement.",
                        ),
                    ),
                ),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverUnsupported)
        assert isinstance(result.evidence, Rejected)
        assert isinstance(result.evidence.detail, MaterialDifference)
        assert not restorer.actions
        with closing(Journal.open(branch)) as journal:
            assert not any(
                isinstance(item.request, (Pause, Settle)) for item in journal.operations()
            )
            requests = journal.requests()
            assert len(requests) == (0 if automatic else 1)
            assert not any(
                isinstance(message, ResultSnapshot)
                for request in requests
                for message in request.input
            )

    asyncio.run(scenario())
