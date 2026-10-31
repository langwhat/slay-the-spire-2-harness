import asyncio
import json
from pathlib import Path
from typing import NoReturn

import httpx2 as httpx
from test_responses import completed, configuration

from sts2_harness.client import models as native
from sts2_harness.client.http import ClientFailureKind, ClientUnavailable
from sts2_harness.client.models import DecisionOwner
from sts2_harness.context import Context
from sts2_harness.context.models import RunResultMessage
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import TextMessage, TextRole, ToolCall
from sts2_harness.material_text import MaterialParts, MaterialText
from sts2_harness.materials import DocumentSource, FileDocuments, ResourceSource
from sts2_harness.notes import Notes
from sts2_harness.tools import ToolCatalog


def test_file_skill_delivery_retains_read_snapshot_through_responses(tmp_path: Path) -> None:
    body = tmp_path / "guide.txt"
    body.write_text("First complete guide.\n", encoding="utf-8")
    resource = tmp_path / "rules.txt"
    resource.write_text("Complete resource.\n", encoding="utf-8")
    source = FileDocuments(
        (
            DocumentSource(
                id="guide",
                name="Guide",
                description="A guide",
                path=body,
                resources=(ResourceSource(key="rules", description="Rules", path=resource),),
            ),
        )
    )
    prompt = tmp_path / "system.txt"
    prompt.write_text("Synthetic system instructions.", encoding="utf-8")
    named_prompt = MaterialText.read("prompt:combat", prompt)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=completed(), headers={"Content-Type": "text/event-stream"}
        )

    async def scenario() -> None:
        async def unavailable() -> NoReturn:
            raise ClientUnavailable(ClientFailureKind.CONNECTION)

        async def unavailable_query(query: native.InspectQuery | native.CatalogRequest) -> NoReturn:
            raise AssertionError("This skill read needs no native catalog or inspection.")

        tools = ToolCatalog(
            DecisionOwner.COMBAT,
            observe=unavailable,
            inspect=unavailable_query,
            catalog=unavailable_query,
            skills=source,
        )
        context = Context(
            DecisionOwner.COMBAT,
            system=(
                TextMessage(
                    TextRole.SYSTEM, MaterialParts((named_prompt, "\nCurrent rules apply."))
                ),
            ),
        )
        call = ToolCall("read", "combat_skill", '{"mode":"read","id":"guide"}')
        first = await tools.dispatch(call)
        context.append_tool_result(call, first)
        old_input = context.model_input()
        message = old_input[-1]
        assert isinstance(message, RunResultMessage)
        document = message.content.data
        assert isinstance(document, DocumentView)
        assert document.text.body == "First complete guide.\n"
        first_version = document.text.version
        body.write_text("Second complete guide.\n", encoding="utf-8")
        second = await tools.dispatch(call)
        context.append_tool_result(call, second)
        assert document.text.body == "First complete guide.\n"
        assert isinstance(second.data, DocumentView)
        assert second.data.text.body == "Second complete guide.\n"
        assert second.data.text.version != first_version
        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(handler)) as llm:
            await llm.complete(old_input, tools.definitions)
            await llm.complete(context.model_input(), tools.definitions)
        assert next(
            tool for tool in tools.definitions if tool.name == "combat_skill"
        ).description.id == ("tool:combat_skill:description")
        system = context.model_input()[0]
        assert isinstance(system, TextMessage) and isinstance(system.value, MaterialParts)
        assert system.value.parts[0] is named_prompt
        first_wire = json.loads(requests[0].content)
        assert json.loads(first_wire["input"][1]["output"])["data"] == {
            "id": "guide",
            "name": "Guide",
            "text": "First complete guide.\n",
            "resources": [{"key": "rules", "description": "Rules"}],
        }
        second_wire = json.loads(requests[1].content)
        assert (
            json.loads(second_wire["input"][-1]["output"])["data"]["text"]
            == "Second complete guide.\n"
        )

    asyncio.run(scenario())


def test_shared_file_supplements_and_resources_keep_complete_independent_identity(
    tmp_path: Path,
) -> None:
    from sts2_harness.client import ModClient
    from sts2_harness.knowledge import KnowledgeLookup, SkillRead, SubjectId

    body = tmp_path / "guide.txt"
    body.write_text("Full shared guide", encoding="utf-8")
    resource = tmp_path / "rules.txt"
    resource.write_text("Full resource", encoding="utf-8")
    subjects = (
        SubjectId(category="relic", definition_id="RELIC.ONE"),
        SubjectId(category="relic", definition_id="RELIC.TWO"),
    )
    source = FileDocuments(
        (
            DocumentSource(
                id="shared",
                name="Shared",
                description="Shared instructions",
                path=body,
                resources=(ResourceSource(key="rules", description="Rules", path=resource),),
                subjects=subjects,
            ),
        )
    )

    async def scenario() -> None:
        found = await source.lookup(subjects)
        assert len(found) == 1
        explicit = await source.read(SkillRead("shared"))
        reread = await source.read(SkillRead("shared"))
        extra = await source.read(SkillRead("shared", "rules"))
        assert found[0].view() == explicit == reread
        assert extra.text.body == "Full resource"
        assert extra.resource == "rules"
        assert extra.text.id != explicit.text.id
        assert extra.name.id == explicit.name.id
        assert json.loads(extra.model_dump_json()) == {
            "id": "shared",
            "name": "Shared",
            "text": "Full resource",
            "resources": [{"key": "rules", "description": "Rules"}],
            "resource": "rules",
        }

        async def observe() -> native.Observation:
            raise ClientUnavailable(ClientFailureKind.CONNECTION)

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "data": {
                        "entries": [
                            {
                                "category": "relic",
                                "definition_id": subject.definition_id,
                                "name": "Native relic",
                                "native": {
                                    "definition_id": subject.definition_id,
                                    "name": "Native relic",
                                    "text": "Native rule",
                                    "rarity": None,
                                },
                            }
                            for subject in subjects
                        ],
                        "missing_ids": [],
                    },
                },
            )

        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            tools = ToolCatalog(
                DecisionOwner.RUN,
                notes=Notes(""),
                observe=observe,
                inspect=client.inspect,
                catalog=client.catalog,
                supplements=source,
                skills=source,
            )
            call = ToolCall(
                "lookup", "run_knowledge", '{"mode":"lookup","ids":["RELIC.ONE","RELIC.TWO"]}'
            )
            result = await tools.dispatch(call)
            assert isinstance(result.data, KnowledgeLookup)
            assert [entry.supplements for entry in result.data.entries] == [
                (explicit,),
                (explicit,),
            ]
            assert all(
                entry.definition.native.text == "Native rule" for entry in result.data.entries
            )
            context = Context(DecisionOwner.RUN)
            context.append_tool_result(call, result)
            context.append_materials((explicit,))
            assert len(context.model_input()) == 1
            message = context.model_input()[0]
            assert isinstance(message, RunResultMessage)
            assert message.content.data == result.data
            assert (
                json.loads(message.text)["data"]["entries"][1]["supplements"][0]["text"]
                == "Full shared guide"
            )

    asyncio.run(scenario())


def test_skill_queries_reject_unadvertised_reads_and_report_real_io_failure(tmp_path: Path) -> None:
    from sts2_harness.client.http import CatalogResult, InspectResult
    from sts2_harness.knowledge import SkillList
    from sts2_harness.results import Failed, ReadCompleted, Rejected

    source = FileDocuments(
        (
            DocumentSource(
                id="missing",
                name="Missing",
                description="Configured but unreadable",
                path=tmp_path / "secret-location.txt",
            ),
        )
    )

    async def observe() -> native.Observation:
        raise ClientUnavailable(ClientFailureKind.CONNECTION)

    async def inspect(query: native.InspectQuery) -> InspectResult:
        raise AssertionError("No inspection is required.")

    async def catalog(query: native.CatalogRequest) -> CatalogResult:
        raise AssertionError("No catalog is required.")

    async def scenario() -> None:
        empty = ToolCatalog(
            DecisionOwner.RUN, notes=Notes(""), observe=observe, inspect=inspect, catalog=catalog
        )
        listed = await empty.dispatch(ToolCall("list", "run_skill", '{"mode":"list"}'))
        assert isinstance(listed.outcome, ReadCompleted)
        assert isinstance(listed.data, SkillList) and listed.data.skills == ()
        enabled = ToolCatalog(
            DecisionOwner.RUN,
            notes=Notes(""),
            observe=observe,
            inspect=inspect,
            catalog=catalog,
            skills=source,
        )
        for arguments in (
            '{"mode":"read","id":"unknown"}',
            '{"mode":"read","id":"missing","resource":"../../secret"}',
            '{"mode":"read","id":"missing","resource":""}',
            '{"mode":"read","id":"missing","resource":null}',
            '{"mode":"read","id":"missing","path":"secret"}',
            '{"mode":"read","id":"missing","id":"unknown"}',
        ):
            rejected = await enabled.dispatch(ToolCall("bad", "run_skill", arguments))
            assert isinstance(rejected.outcome, Rejected)
        failed = await enabled.dispatch(
            ToolCall("io", "run_skill", '{"mode":"read","id":"missing"}')
        )
        assert isinstance(failed.outcome, Failed)
        assert failed.outcome.reason.message == "Document 'missing' could not be read."
        assert failed.observation is None

    asyncio.run(scenario())


def test_initial_guidance_and_explicit_reads_share_actual_delivery_without_losing_versions(
    tmp_path: Path,
) -> None:
    from sts2_harness.client.http import CatalogResult, InspectResult
    from sts2_harness.knowledge import GuidanceContent, SkillRead

    path = tmp_path / "guide.txt"
    path.write_text("Version A", encoding="utf-8")
    resource = tmp_path / "resource.txt"
    resource.write_text("Resource A", encoding="utf-8")
    source = FileDocuments(
        (
            DocumentSource(
                id="guide",
                name="Guide",
                description="Guidance",
                path=path,
                resources=(ResourceSource(key="rules", description="Rules", path=resource),),
            ),
        )
    )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=completed(), headers={"Content-Type": "text/event-stream"}
        )

    async def observe() -> native.Observation:
        raise ClientUnavailable(ClientFailureKind.CONNECTION)

    async def inspect(query: native.InspectQuery) -> InspectResult:
        raise AssertionError("No inspection is required.")

    async def catalog(query: native.CatalogRequest) -> CatalogResult:
        raise AssertionError("No catalog is required.")

    async def scenario() -> None:
        initial = await source.read(SkillRead("guide"))
        context = Context(
            DecisionOwner.COMBAT,
            system=(TextMessage(TextRole.SYSTEM, GuidanceContent(guidance=(initial,))),),
        )
        original = context.model_input()
        context.append_materials((initial,))
        assert context.model_input() == original
        tools = ToolCatalog(
            DecisionOwner.COMBAT, observe=observe, inspect=inspect, catalog=catalog, skills=source
        )
        call = ToolCall("explicit", "combat_skill", '{"mode":"read","id":"guide"}')
        context.append_tool_result(call, await tools.dispatch(call))
        explicit = context.model_input()[-1]
        assert isinstance(explicit, RunResultMessage)
        assert explicit.content.data == initial
        context.append_materials((initial,))
        assert len(context.model_input()) == 2
        path.write_text("Version B", encoding="utf-8")
        changed = await source.read(SkillRead("guide"))
        context.append_materials((changed,))
        assert len(context.model_input()) == 3
        assert original[0].text == json.dumps(
            {
                "guidance": [
                    {
                        "id": "guide",
                        "name": "Guide",
                        "text": "Version A",
                        "resources": [{"key": "rules", "description": "Rules"}],
                    }
                ]
            },
            separators=(",", ":"),
        )
        path.write_text("Version A", encoding="utf-8")
        returned = await source.read(SkillRead("guide"))
        context.append_materials((returned,))
        assert len(context.model_input()) == 4
        context.append_materials((returned,))
        assert len(context.model_input()) == 4
        extra = await source.read(SkillRead("guide", "rules"))
        resource_call = ToolCall(
            "resource", "combat_skill", '{"mode":"read","id":"guide","resource":"rules"}'
        )
        context.append_tool_result(resource_call, await tools.dispatch(resource_call))
        context.append_materials((returned,))
        assert len(context.model_input()) == 5
        assert extra.text.id != returned.text.id
        fresh = Context(DecisionOwner.COMBAT)
        fresh.append_materials(())
        assert fresh.model_input() == ()
        fresh.append_materials((returned,))
        message = fresh.model_input()[0]
        assert isinstance(message, TextMessage) and message.role is TextRole.USER
        assert isinstance(message.value, GuidanceContent)
        assert message.value.guidance == (returned,)
        assert message.value.guidance[0].text.version == initial.text.version
        resource_first = Context(DecisionOwner.COMBAT)
        resource_first.append_tool_result(resource_call, await tools.dispatch(resource_call))
        resource_first.append_materials((returned,))
        assert len(resource_first.model_input()) == 2
        list_first = Context(DecisionOwner.COMBAT)
        list_call = ToolCall("list", "combat_skill", '{"mode":"list"}')
        list_first.append_tool_result(list_call, await tools.dispatch(list_call))
        list_first.append_materials((returned,))
        assert len(list_first.model_input()) == 2
        renamed = DocumentView(
            id=returned.id,
            name=MaterialText.create(returned.name.id, "Renamed guide"),
            text=returned.text,
            resources=returned.resources,
        )
        context.append_materials((renamed,))
        assert len(context.model_input()) == 6
        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(handler)) as llm:
            await llm.complete(context.model_input(), tools.definitions)
        wire = json.loads(requests[0].content)["input"]
        assert [item.get("role") for item in wire] == [
            "system",
            None,
            "user",
            "user",
            None,
            "user",
        ]
        assert json.loads(wire[0]["content"]) == {
            "guidance": [
                {
                    "id": "guide",
                    "name": "Guide",
                    "text": "Version A",
                    "resources": [{"key": "rules", "description": "Rules"}],
                }
            ]
        }
        assert json.loads(wire[2]["content"])["guidance"][0]["text"] == "Version B"
        assert json.loads(wire[3]["content"])["guidance"][0]["text"] == "Version A"
        assert json.loads(wire[5]["content"])["guidance"][0]["name"] == "Renamed guide"

    asyncio.run(scenario())


def test_document_failures_identify_the_logical_source_for_reads_and_lookup(tmp_path: Path) -> None:
    import pytest

    from sts2_harness.knowledge import SkillRead, SubjectId, UnknownDocument
    from sts2_harness.material_text import MaterialUnavailable

    subject = SubjectId(category="encounter", definition_id="ENCOUNTER.TEST")
    documents = FileDocuments(
        (
            DocumentSource(
                "boss-guide", "Boss", "Guidance", tmp_path / "private.txt", subjects=(subject,)
            ),
        )
    )

    async def scenario() -> None:
        for lookup in (False, True):
            with pytest.raises(MaterialUnavailable) as failure:
                if lookup:
                    await documents.lookup((subject,))
                else:
                    await documents.read(SkillRead("boss-guide"))
            assert str(failure.value) == "Document 'boss-guide' could not be read."
            assert isinstance(failure.value.__cause__, MaterialUnavailable)
            assert str(tmp_path) not in str(failure.value)
        with pytest.raises(UnknownDocument, match="unknown-guide"):
            await documents.read(SkillRead("unknown-guide"))

    asyncio.run(scenario())
