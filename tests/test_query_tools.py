import asyncio

import httpx2 as httpx
import pytest
from pydantic import MISSING
from test_mod_client import COMBAT

from sts2_harness.client import ModClient
from sts2_harness.client.models import DecisionOwner
from sts2_harness.context import Context
from sts2_harness.context.models import ResultMessage
from sts2_harness.execution import Execution
from sts2_harness.game import LiveGame
from sts2_harness.llm.messages import ToolCall
from sts2_harness.material_text import MaterialText
from sts2_harness.notes import Notes
from sts2_harness.results import ReadCompleted
from sts2_harness.tools import ToolCatalog


def test_state_dispatch_delivers_current_state_without_query_data() -> None:
    async def run() -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET" and request.url.path == "/state"
            return httpx.Response(
                200, content='{"status":"completed","observation":' + COMBAT + "}"
            )

        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="query-test", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
            )
            tools = ToolCatalog(
                DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
            )
            call = ToolCall("state", "combat_inspect", '{"area":"state"}')
            result = await tools.dispatch(call)
            assert isinstance(result.outcome, ReadCompleted)
            assert result.data is MISSING
            context = Context(DecisionOwner.COMBAT)
            context.append_tool_result(call, result)
            message = context.model_input()[-1]
            assert isinstance(message, ResultMessage)
            assert message.content.state is not None
            assert message.content.data is MISSING
            assert len(tools.definitions) == 4

    asyncio.run(run())


def test_inspection_keeps_full_details_and_rejects_bad_arguments_and_foreign_menus() -> None:
    from test_mod_client import RUN

    from sts2_harness.client.models import DetailData, Observation, UpgradeAvailable
    from sts2_harness.results import Rejected

    async def run() -> None:
        observation = Observation.model_validate_json(COMBAT)
        details = DetailData(
            objects={"c1": observation.objects["c1"]},
            descriptions={"c1": observation.descriptions["c1"]},
            modifier_descriptions={"c1": observation.modifier_descriptions["c1"]},
            generated={"c1": observation.generated["c1"]},
            mechanisms=(),
            upgrades={"c1": observation.upgrades["c1"]},
        )
        paths: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/state":
                return httpx.Response(
                    200, content='{"status":"completed","observation":' + COMBAT + "}"
                )
            if b"bad" in request.content:
                return httpx.Response(
                    200,
                    content='{"status":"rejected","error":'
                    '{"code":"invalid_refs","message":"Wrong area.","invalid_refs":["bad"]},'
                    '"observation":' + COMBAT + "}",
                )
            if b"options" in request.content:
                return httpx.Response(
                    200,
                    content='{"status":"completed","observation":'
                    + RUN
                    + ',"data":{"objects":{},"descriptions":{},"modifier_descriptions":{},'
                    '"generated":{},"mechanisms":[]}}',
                )
            return httpx.Response(
                200,
                content='{"status":"completed","observation":'
                + COMBAT
                + ',"data":'
                + details.model_dump_json()
                + "}",
            )

        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="query-test", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
            )
            tools = ToolCatalog(
                DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
            )
            context = Context(DecisionOwner.COMBAT)
            call = ToolCall(
                "detail", "combat_inspect", '{"area":"hand","refs":["c1"],"upgrade":true}'
            )
            for _ in range(2):
                result = await tools.dispatch(call)
                assert isinstance(result.data, DetailData)
                assert result.data.descriptions == {"c1": None}
                assert result.data.modifier_descriptions["c1"].enchantment == "附魔说明"
                assert result.data.upgrades is not MISSING
                upgrade = result.data.upgrades["c1"]
                assert isinstance(upgrade, UpgradeAvailable)
                assert upgrade.card.text == "造成3点伤害。"
                assert len(result.data.generated["c1"]) == 1
                context.append_tool_result(call, result)
            assert paths == ["/inspect", "/inspect"]
            last = context.model_input()[-1]
            assert isinstance(last, ResultMessage) and isinstance(last.content.data, DetailData)
            assert last.content.updates.to_json() == "{}"
            bad = await tools.dispatch(
                ToolCall("bad", "combat_inspect", '{"area":"hand","refs":["bad"]}')
            )
            assert isinstance(bad.outcome, Rejected) and bad.observation is not None
            assert "bad" in bad.outcome.reason.message
            denied = await tools.dispatch(ToolCall("other", "combat_inspect", '{"area":"options"}'))
            assert isinstance(denied.outcome, Rejected) and denied.data is MISSING
            assert denied.observation is not None
            assert denied.observation.location.owner is DecisionOwner.RUN
            for arguments in [
                '{"area":"hand","refs":[]}',
                '{"area":"hand","refs":["c1","c1"]}',
                '{"area":"powers","refs":["e1"]}',
                '{"area":"state","upgrade":false}',
                '{"area":"hand","refs":null}',
                '{"area":"state","area":"hand"}',
            ]:
                result = await tools.dispatch(ToolCall("invalid", "combat_inspect", arguments))
                assert isinstance(result.outcome, Rejected) and result.data is MISSING
            wrong_name = await tools.dispatch(ToolCall("wrong", "run_inspect", '{"area":"state"}'))
            assert isinstance(wrong_name.outcome, Rejected)

    asyncio.run(run())


def test_knowledge_preserves_native_matches_pagination_and_lookup_order() -> None:
    import json

    from sts2_harness.client.models import LookupData, SearchData
    from sts2_harness.results import Rejected

    async def run() -> None:
        requests: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/state":
                return httpx.Response(
                    200, content='{"status":"completed","observation":' + COMBAT + "}"
                )
            body = request.content.decode()
            requests.append(body)
            mode = json.loads(body)["mode"]
            if mode == "catalog":
                data = (
                    '{"categories":["relic"],"card_filters":{"pools":[],"types":[],'
                    '"rarities":[],"energy_costs":[0,"X"]}}'
                )
            elif mode == "search":
                data = (
                    '{"matches":[{"category":"relic","definition_id":"RELIC.ONE",'
                    '"name":"遗物","description":"完整原生正文"}],"total":8,"next_offset":5}'
                )
            else:
                data = (
                    '{"entries":[{"category":"relic","definition_id":"RELIC.ONE",'
                    '"name":"遗物","native":{"definition_id":"RELIC.ONE","name":"遗物",'
                    '"text":"完整原生正文","rarity":null}}],"missing_ids":["MISSING"]}'
                )
            return httpx.Response(200, content='{"status":"completed","data":' + data + "}")

        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="query-test", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
            )
            tools = ToolCatalog(
                DecisionOwner.RUN,
                notes=Notes(""),
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
            )
            result = await tools.dispatch(
                ToolCall("catalog", "run_knowledge", '{"mode":"catalog"}')
            )
            assert isinstance(result.outcome, ReadCompleted) and result.observation is not None
            result = await tools.dispatch(ToolCall("search", "run_knowledge", '{"mode":"search"}'))
            assert isinstance(result.data, SearchData)
            assert result.data.matches[0].definition_id == "RELIC.ONE"
            assert result.data.total == 8 and result.data.next_offset == 5
            assert requests[-1] == '{"mode":"search"}'
            lookup = await tools.dispatch(
                ToolCall(
                    "lookup", "run_knowledge", '{"mode":"lookup","ids":["MISSING","RELIC.ONE"]}'
                )
            )
            assert not isinstance(lookup.data, LookupData)
            assert lookup.data is not MISSING
            # Model-facing lookup adds supplements without changing the native result.
            encoded = json.loads(lookup.data.model_dump_json())
            assert encoded["entries"][0]["definition_id"] == "RELIC.ONE"
            assert encoded["entries"][0]["supplements"] == []
            assert encoded["missing_ids"] == ["MISSING"]
            before = len(requests)
            for invalid in [
                '{"mode":"search","filters":{}}',
                '{"mode":"search","category":"relic","filters":{}}',
                '{"mode":"search","category":"card","filters":{"types":[]}}',
                '{"mode":"search","limit":21}',
                '{"mode":"lookup","ids":["a","a"]}',
                '{"mode":"search","category":null}',
            ]:
                bad = await tools.dispatch(ToolCall("invalid", "run_knowledge", invalid))
                assert isinstance(bad.outcome, Rejected)
            assert len(requests) == before
            await tools.dispatch(
                ToolCall(
                    "filter",
                    "run_knowledge",
                    '{"mode":"search","category":"card","filters":{},"limit":20,"offset":5}',
                )
            )
            assert '"filters":{}' in requests[-1]

    asyncio.run(run())


def test_supplements_keep_document_identity_and_explicit_reads_and_failures() -> None:
    import json

    from sts2_harness.knowledge import (
        Document,
        KnowledgeLookup,
        Resource,
        SubjectId,
        SupplementUnavailable,
    )
    from sts2_harness.results import Failed

    class Provider:
        def __init__(self) -> None:
            self.failure: Exception | None = None
            self.requests: list[tuple[SubjectId, ...]] = []

        async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
            self.requests.append(subjects)
            if self.failure is not None:
                raise self.failure
            doc = Document(
                id="guide",
                name=MaterialText.create("guide:name", "Guide"),
                text=MaterialText.create("guide:text", "Complete supplemental text."),
                resources=(
                    Resource(key="rules", description=MaterialText.create("guide:rules", "Rules")),
                ),
                subjects=subjects,
            )
            return (doc, doc)

    async def run() -> None:
        provider = Provider()

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/state":
                return httpx.Response(
                    200, content='{"status":"completed","observation":' + COMBAT + "}"
                )
            return httpx.Response(
                200,
                content='{"status":"completed","data":{"entries":['
                '{"category":"relic","definition_id":"RELIC.ONE","name":"遗物",'
                '"native":{"definition_id":"RELIC.ONE","name":"遗物","text":"原生","rarity":null}},'
                '{"category":"relic","definition_id":"RELIC.TWO","name":"另一遗物",'
                '"native":{"definition_id":"RELIC.TWO","name":"另一遗物","text":"规则","rarity":null}}],'
                '"missing_ids":["MISSING"]}}',
            )

        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="query-test", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
            )
            tools = ToolCatalog(
                DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                supplements=provider,
            )
            context = Context(DecisionOwner.COMBAT)
            call = ToolCall(
                "lookup",
                "combat_knowledge",
                '{"mode":"lookup","ids":["RELIC.ONE","RELIC.TWO","MISSING"]}',
            )
            for _ in range(2):
                result = await tools.dispatch(call)
                assert isinstance(result.data, KnowledgeLookup)
                assert result.data.missing_ids == ("MISSING",)
                assert len(result.data.entries) == 2
                assert len(result.data.entries[0].supplements) == 1
                assert result.data.entries[0].supplements == result.data.entries[1].supplements
                assert result.data.entries[0].definition.definition_id == "RELIC.ONE"
                context.append_tool_result(call, result)
            assert provider.requests == [
                (
                    SubjectId(category="relic", definition_id="RELIC.ONE"),
                    SubjectId(category="relic", definition_id="RELIC.TWO"),
                ),
                (
                    SubjectId(category="relic", definition_id="RELIC.ONE"),
                    SubjectId(category="relic", definition_id="RELIC.TWO"),
                ),
            ]
            encoded = json.loads(context.model_input()[-1].text)
            assert encoded["data"]["entries"][0]["supplements"] == [
                {
                    "id": "guide",
                    "name": "Guide",
                    "text": "Complete supplemental text.",
                    "resources": [{"key": "rules", "description": "Rules"}],
                }
            ]
            assert encoded["updates"] == {}
            provider.failure = SupplementUnavailable("Example source could not be read.")
            failed = await tools.dispatch(call)
            assert isinstance(failed.outcome, Failed)
            assert failed.observation is not None and failed.data is MISSING
            assert "Example source" in failed.outcome.reason.message
            provider.failure = OSError("/private/example/source")
            failed = await tools.dispatch(call)
            assert isinstance(failed.outcome, Failed) and failed.observation is not None
            assert "/private" not in failed.outcome.reason.message
            provider.failure = ValueError("Provider invariant failed")
            import pytest

            with pytest.raises(ValueError, match="Provider invariant failed"):
                await tools.dispatch(call)

    asyncio.run(run())


def test_failed_reads_preserve_trustworthy_state_without_reusing_old_observations() -> None:
    from sts2_harness.results import Failed, Rejected

    async def run() -> None:
        unavailable = False

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/state":
                if unavailable:
                    raise httpx.ConnectError("Unavailable", request=request)
                return httpx.Response(
                    200, content='{"status":"completed","observation":' + COMBAT + "}"
                )
            if request.url.path == "/catalog":
                raise httpx.ReadTimeout("Unavailable", request=request)
            return httpx.Response(
                200,
                content='{"status":"failed","error":{"code":"observation_changed",'
                '"message":"The publication changed."},"observation":' + COMBAT + "}",
            )

        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="query-test", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
            )
            tools = ToolCatalog(
                DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
            )
            failed = await tools.dispatch(ToolCall("detail", "combat_inspect", '{"area":"hand"}'))
            assert isinstance(failed.outcome, Failed)
            assert failed.observation is not None and failed.data is MISSING
            failed = await tools.dispatch(
                ToolCall("catalog", "combat_knowledge", '{"mode":"catalog"}')
            )
            assert isinstance(failed.outcome, Failed) and failed.observation is not None
            unavailable = True
            failed = await tools.dispatch(ToolCall("state", "combat_inspect", '{"area":"state"}'))
            assert isinstance(failed.outcome, Failed) and failed.observation is None
            invalid = await tools.dispatch(ToolCall("bad", "combat_inspect", "{}"))
            assert isinstance(invalid.outcome, Rejected) and invalid.observation is None

    asyncio.run(run())


@pytest.mark.parametrize("transport_failure", [True, False])
@pytest.mark.parametrize("state_available", [True, False])
def test_inspect_failure_without_observation_refreshes_state_or_ends_the_run(
    transport_failure: bool,
    state_available: bool,
) -> None:

    from sts2_harness.execution import GameLifetimeEnded
    from sts2_harness.results import Failed

    async def run() -> None:
        paths: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/state":
                if not state_available:
                    return httpx.Response(
                        200,
                        content='{"status":"failed","error":{"code":"no_run",'
                        '"message":"No run is active."}}',
                    )
                return httpx.Response(
                    200, content='{"status":"completed","observation":' + COMBAT + "}"
                )
            if transport_failure:
                raise httpx.ReadTimeout("Unavailable", request=request)
            return httpx.Response(
                200,
                content='{"status":"failed","error":{"code":"read_unavailable",'
                '"message":"Details are unavailable."},"observation":null}',
            )

        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="query-test", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
            )
            tools = ToolCatalog(
                DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
            )
            call = ToolCall("detail", "combat_inspect", '{"area":"hand"}')
            if not state_available:
                with pytest.raises(GameLifetimeEnded):
                    await tools.dispatch(call)
            else:
                result = await tools.dispatch(call)
                assert isinstance(result.outcome, Failed)
                assert result.observation is not None and result.data is MISSING
            assert paths == ["/inspect", "/state"]

    asyncio.run(run())
