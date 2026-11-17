import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import BaseModel, Field, RootModel
from test_responses import InspectArguments, configuration

from sts2_harness.context.models import RunProjectedResult, RunResultMessage
from sts2_harness.journal import SessionMaterials
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    FailureKind,
    ModelUnavailable,
    TextMessage,
    TextRole,
    ToolDefinition,
)
from sts2_harness.material_text import MaterialText
from sts2_harness.results import QueryToolResult, ReadCompleted


def test_messages_real_tool_object_unions_keep_their_schema_on_wire(tmp_path: Path) -> None:
    from typing import Annotated, Literal

    from sts2_harness.tools.inputs import (
        CombatActInput,
        CombatInspectInput,
        KnowledgeInput,
        NotesInput,
        RunActInput,
        SkillInput,
    )

    class First(BaseModel):
        kind: Literal["first"]
        value: str

    class Second(BaseModel):
        kind: Literal["second"]
        count: int

    class ReferencedUnion(RootModel[Annotated[First | Second, Field(discriminator="kind")]]):
        pass

    parameters: dict[str, type[BaseModel]] = {
        "combat_inspect": CombatInspectInput,
        "combat_act": CombatActInput,
        "run_act": RunActInput,
        "combat_knowledge": KnowledgeInput,
        "combat_skill": SkillInput,
        "run_notes": NotesInput,
        "referenced_union": ReferencedUnion,
        "referenced_anyof": RootModel[First | Second],
        "typed_object": InspectArguments,
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        for tool in body["tools"]:
            if tool["input_schema"].get("type") != "object":
                return httpx.Response(
                    400,
                    json={
                        "error": {"type": "invalid_request_error", "message": "Object required."}
                    },
                )
        stream = events(stop="end_turn")
        return httpx.Response(
            200,
            content=sse(stream[:1] + stream[-2:]),
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages", LLM_EXTRA_RETRIES="0"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            await llm.complete(
                (TextMessage(TextRole.USER, "Inspect the synthetic state."),),
                tuple(
                    ToolDefinition(name, MaterialText.create(name, "Synthetic tool."), model)
                    for name, model in parameters.items()
                ),
            )

    asyncio.run(scenario())
    encoded = {
        tool["name"]: tool["input_schema"] for tool in json.loads(requests[0].content)["tools"]
    }
    for name, schema in encoded.items():
        original = parameters[name].model_json_schema()
        assert schema == {**original, "type": "object"}
    referenced = encoded["referenced_union"]
    assert referenced["discriminator"]["propertyName"] == "kind"
    assert referenced["oneOf"] == [{"$ref": "#/$defs/First"}, {"$ref": "#/$defs/Second"}]
    assert set(referenced["$defs"]) == {"First", "Second"}


def test_role_clients_share_injected_transport_without_closing_it_twice(tmp_path: Path) -> None:
    from sts2_harness.model_purpose import ModelPurpose

    class CountingTransport(httpx.MockTransport):
        closes = 0

        async def aclose(self) -> None:
            self.closes += 1
            await super().aclose()

    def handler(request: httpx.Request) -> httpx.Response:
        stream = events(stop="end_turn")
        return httpx.Response(
            200,
            content=sse(stream[:1] + stream[-2:]),
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> None:
        transport = CountingTransport(handler)
        async with Llm(
            configuration(
                tmp_path,
                LLM_PROTOCOL="messages",
                LLM_REASONING_EFFORT="low",
                LLM_REASONING_EFFORT_DECK="medium",
                LLM_REASONING_EFFORT_COMBAT="high",
            ),
            transport=transport,
        ) as llm:
            await llm.complete((), ())
            await llm.for_purpose(ModelPurpose.DECK).complete((), ())
            await llm.for_purpose(ModelPurpose.COMBAT).complete((), ())
            assert transport.closes == 0
        assert transport.closes == 1

    asyncio.run(scenario())


def sse(events: list[dict[str, object]]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def events(*, stop: str = "tool_use") -> list[dict[str, object]]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "reported",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "opaque": {"keep": None},
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 70,
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": "", "opaque": None},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Synthetic thought."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "synthetic-signature"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "redacted_thinking", "data": "synthetic-redacted"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "call_1",
                "name": "inspect",
                "input": {},
                "opaque": {"null": None},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"reference":'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '"c1"}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 5, "service_tier": "standard"},
        },
        {"type": "message_stop"},
    ]


def test_messages_native_wire_and_recorded_continuation(tmp_path: Path) -> None:
    from sts2_harness.client import models as native
    from sts2_harness.journal import Journal, Manifest
    from sts2_harness.journal_models import AssistantSnapshot
    from sts2_harness.model_journal import RecordedLlm

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=sse(events()), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "messages.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="messages",
                core_version="test",
                game_instance_id="instance",
                run_id="run",
            ),
        )
        tools = (
            ToolDefinition(
                "inspect", MaterialText.create("inspect", "Inspect a card."), InspectArguments
            ),
        )
        history = (
            TextMessage(TextRole.SYSTEM, "Use available tools."),
            TextMessage(TextRole.USER, "Inspect."),
        )
        async with Llm(
            configuration(
                tmp_path,
                LLM_PROTOCOL="messages",
                LLM_REASONING_EFFORT="high",
                LLM_SERVICE_TIER="standard_only",
                LLM_BASE_URL="https://example.invalid/proxy",
            ),
            transport=httpx.MockTransport(handler),
        ) as llm:
            recorded = RecordedLlm(
                llm, journal, role=native.DecisionOwner.COMBAT, stream="combat:1"
            )
            reply = await recorded.complete(history, tools)
            assert reply.assistant.text == ""
            assert reply.assistant.tool_calls[0].arguments == '{"reference":"c1"}'
            assert reply.usage.input_tokens == 100 and reply.usage.cache_hit_ratio == 0.7
            assert reply.metadata.response_service_tier == "standard"
            await recorded.complete(
                (
                    *history,
                    reply.assistant,
                    RunResultMessage(
                        "call_1",
                        RunProjectedResult(outcome=ReadCompleted(), observation=None),
                        "Frozen tool result: 中文.",
                        QueryToolResult(ReadCompleted(), None),
                    ),
                    TextMessage(TextRole.USER, "Auxiliary."),
                ),
                tools,
            )
        first, second = map(lambda request: json.loads(request.content), requests)
        assert requests[0].url.path == "/proxy/v1/messages"
        assert first["max_tokens"] == 16000 and "thinking" not in first
        assert first["cache_control"] == {"type": "ephemeral"}
        assert (
            first["output_config"] == {"effort": "high"}
            and first["service_tier"] == "standard_only"
        )
        assert first["system"] == [{"type": "text", "text": "Use available tools."}]
        assistant = second["messages"][1]
        assert assistant == {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Synthetic thought.",
                    "signature": "synthetic-signature",
                    "opaque": None,
                },
                {"type": "redacted_thinking", "data": "synthetic-redacted"},
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "inspect",
                    "input": {"reference": "c1"},
                    "opaque": {"null": None},
                },
            ],
        }
        assert second["messages"][2] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "Frozen tool result: 中文.",
                }
            ],
        }
        assert second["messages"][3] == {"role": "user", "content": "Auxiliary."}
        journal.close()
        archive = Journal.open(journal.path)
        one, two = archive.requests()
        retained = one.attempts[0].reply
        assert retained is not None and retained.protocol.value == "messages"
        raw = json.loads(retained.native_output)
        assert raw["opaque"] == {"keep": None} and "stop_details" not in raw
        assert raw["content"] == assistant["content"]
        saved = two.input[-3]
        assert (
            isinstance(saved, AssistantSnapshot)
            and saved.native_reply.to_json() == retained.native_output
        )
        assert two.base_sequence == one.sequence
        archive.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stop",
    ["max_tokens", "model_context_window_exceeded", "pause_turn", "refusal", "stop_sequence"],
)
def test_messages_terminal_failures_never_deliver_tools(tmp_path: Path, stop: str) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=sse(events(stop=stop)), headers={"content-type": "text/event-stream"}
            )

        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages", LLM_EXTRA_RETRIES="0"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is (
                FailureKind.TRUNCATED
                if stop in {"max_tokens", "model_context_window_exceeded"}
                else FailureKind.PROTOCOL
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("initial_usage", "output_tokens"),
    [({"input_tokens": 10, "output_tokens": 0}, 5), ({}, None)],
    ids=["partial", "unknown"],
)
def test_messages_unknown_cache_counts_and_defaults_do_not_become_zero(
    tmp_path: Path, initial_usage: dict[str, object], output_tokens: int | None
) -> None:
    stream = events()
    stream[0]["message"] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "reported",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": initial_usage,
    }
    stream[-2]["usage"] = {"output_tokens": output_tokens}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=sse(stream), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages"), transport=httpx.MockTransport(handler)
        ) as llm:
            reply = await llm.complete((), ())
            assert reply.usage.input_tokens is None and reply.usage.cache_hit_ratio is None
            assert reply.usage.cache_write_tokens is None and reply.usage.cache_read_tokens is None
            assert reply.usage.output_tokens == output_tokens
            raw = json.loads(reply.assistant.native.to_json())
            assert "cache_creation_input_tokens" not in raw["usage"] and "container" not in raw
        wire = json.loads(requests[0].content)
        assert "thinking" not in wire and "output_config" not in wire and "service_tier" not in wire

    asyncio.run(scenario())


def test_messages_complete_arguments_wait_for_message_stop_and_cancellation_closes_stream(
    tmp_path: Path,
) -> None:
    from collections.abc import AsyncIterator

    intermediate = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield sse(events()[:-1])
            intermediate.set()
            await release.wait()
            yield sse([{"type": "message_stop"}])

        async def aclose(self) -> None:
            closed.set()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=GatedStream(), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages"), transport=httpx.MockTransport(handler)
        ) as llm:
            pending = asyncio.create_task(llm.complete((), ()))
            await intermediate.wait()
            assert not pending.done()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert closed.is_set()
            intermediate.clear()
            closed.clear()
            pending = asyncio.create_task(llm.complete((), ()))
            await intermediate.wait()
            assert not pending.done()
            release.set()
            reply = await pending
            assert reply.assistant.tool_calls[0].id == "call_1"

    asyncio.run(scenario())
