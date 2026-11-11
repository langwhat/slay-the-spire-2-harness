import asyncio
import json
from pathlib import Path
from typing import NoReturn

import httpx2 as httpx
import pytest

from sts2_harness.client import models as native
from sts2_harness.client.http import ClientFailureKind, ClientUnavailable
from sts2_harness.client.models import DecisionOwner
from sts2_harness.context import Context
from sts2_harness.context.models import RunResultMessage
from sts2_harness.llm.messages import ToolCall
from sts2_harness.materials import DocumentSource, FileDocuments, ResourceSource
from sts2_harness.notes import Notes
from sts2_harness.tools import ToolCatalog


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
            '{"mode":"list","offset":-1}',
            '{"mode":"list","limit":0}',
            '{"mode":"list","limit":21}',
            '{"mode":"list","limit":"2"}',
            '{"mode":"list","text":null}',
            '{"mode":"read","id":"missing","text":"ignored"}',
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


@pytest.mark.parametrize("role", (DecisionOwner.RUN, DecisionOwner.COMBAT))
def test_skill_discovery_is_bounded_and_searchable(tmp_path: Path, role: DecisionOwner) -> None:
    from sts2_harness.knowledge import SkillList
    from sts2_harness.results import ReadCompleted

    source = FileDocuments(
        tuple(
            DocumentSource(
                id=f"guide:{index:02}",
                name="Queen route" if index == 7 else f"Guide {index}",
                description="CARD.POMMEL_STRIKE access" if index < 3 else "Other effects",
                path=tmp_path / "unread-during-discovery.txt",
            )
            for index in reversed(range(23))
        )
    )

    async def unavailable() -> NoReturn:
        raise ClientUnavailable(ClientFailureKind.CONNECTION)

    async def unavailable_query(query: native.InspectQuery | native.CatalogRequest) -> NoReturn:
        raise AssertionError("Skill discovery needs no native query.")

    async def scenario() -> None:
        tools = ToolCatalog(
            role,
            observe=unavailable,
            inspect=unavailable_query,
            catalog=unavailable_query,
            notes=Notes("") if role is DecisionOwner.RUN else None,
            skills=source,
        )
        tool = "run_skill" if role is DecisionOwner.RUN else "combat_skill"
        first = await tools.dispatch(ToolCall("first", tool, '{"mode":"list"}'))
        assert isinstance(first.outcome, ReadCompleted)
        assert isinstance(first.data, SkillList)
        assert len(first.data.skills) == 20
        assert [skill.id for skill in first.data.skills] == [f"guide:{i:02}" for i in range(20)]
        assert first.data.total == 23
        assert first.data.next_offset == 20
        tail = await tools.dispatch(ToolCall("tail", tool, '{"mode":"list","offset":20}'))
        assert isinstance(tail.data, SkillList)
        assert [skill.id for skill in tail.data.skills] == ["guide:20", "guide:21", "guide:22"]
        assert tail.data.total == 23 and tail.data.next_offset is None

        selected = await tools.dispatch(
            ToolCall("selected", tool, '{"mode":"list","text":"card.pommel_strike","limit":2}')
        )
        assert isinstance(selected.data, SkillList)
        assert [skill.id for skill in selected.data.skills] == ["guide:00", "guide:01"]
        assert selected.data.total == 3 and selected.data.next_offset == 2
        last = await tools.dispatch(
            ToolCall(
                "last", tool, '{"mode":"list","text":"card.pommel_strike","offset":2,"limit":2}'
            )
        )
        assert isinstance(last.data, SkillList)
        assert [skill.id for skill in last.data.skills] == ["guide:02"]
        assert last.data.total == 3 and last.data.next_offset is None

        for search, expected in (
            (" qUeEn ", ["guide:07"]),
            ("guide:22", ["guide:22"]),
            ("missing", []),
        ):
            found = await tools.dispatch(
                ToolCall("match", tool, json.dumps({"mode": "list", "text": search}))
            )
            assert isinstance(found.data, SkillList)
            assert [skill.id for skill in found.data.skills] == expected
            assert found.data.total == len(expected) and found.data.next_offset is None

        beyond = await tools.dispatch(ToolCall("beyond", tool, '{"mode":"list","offset":99}'))
        assert isinstance(beyond.data, SkillList)
        assert beyond.data.skills == () and beyond.data.total == 23
        assert beyond.data.next_offset is None

    asyncio.run(scenario())
