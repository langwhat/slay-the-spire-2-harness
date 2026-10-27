import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import BaseModel, Field, RootModel
from test_responses import InspectArguments, configuration

from sts2_harness.context.models import ProjectedResult, ResultMessage, Updates
from sts2_harness.journal import SessionMaterials
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    FailureKind,
    ModelUnavailable,
    TextMessage,
    TextRole,
    ToolDefinition,
    Usage,
)
from sts2_harness.material_text import MaterialText
from sts2_harness.results import QueryToolResult, ReadCompleted


def test_messages_real_tool_object_unions_keep_their_schema_on_wire(tmp_path: Path) -> None:
    from typing import Annotated, Literal

    from sts2_harness.tools.inputs import (
        CombatActInput,
        InspectInput,
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
        "combat_inspect": InspectInput,
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


@pytest.mark.parametrize("parameters", [RootModel[str], RootModel[InspectArguments | str]])
def test_messages_non_object_tool_inputs_fail_before_wire(
    tmp_path: Path, parameters: type[BaseModel]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Non-object input schema reached provider.")

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages", LLM_EXTRA_RETRIES="0"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete(
                    (),
                    (
                        ToolDefinition(
                            "unsupported", MaterialText.create("tool", "Tool."), parameters
                        ),
                    ),
                )
            assert failure.value.reason is FailureKind.CONFIGURATION

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
                    ResultMessage(
                        "call_1",
                        ProjectedResult(outcome=ReadCompleted(), state=None, updates=Updates()),
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
                    "content": '{"outcome":{"status":"completed"},"state":null,"updates":{}}',
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


def test_messages_combat_closes_all_rejected_calls_in_one_user_turn(tmp_path: Path) -> None:
    from test_combat import BattleHTTP

    from sts2_harness.config import Protocol
    from sts2_harness.decisions import CombatHandoff

    def response(calls: tuple[str, ...] = (), text: str = "") -> bytes:
        stream = events()[:1]
        for index, call in enumerate(calls):
            stream.extend(
                [
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": call,
                            "name": "combat_act",
                            "input": {"steps": [{"kind": "end_turn"}]},
                        },
                    },
                    {"type": "content_block_stop", "index": index},
                ]
            )
        if text:
            stream.extend(
                [
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "text",
                            "text": text,
                            "parsed_output": {"provider": None},
                        },
                    },
                    {"type": "content_block_stop", "index": 0},
                ]
            )
        stream.extend(
            [
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use" if calls else "end_turn"},
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            ]
        )
        return sse(stream)

    battle = BattleHTTP(
        [response(("first", "second")), response(("act",)), response(text="Review complete.")]
    )
    result = asyncio.run(battle.run(tmp_path, protocol=Protocol.MESSAGES))
    assert isinstance(result, CombatHandoff) and result.review == "Review complete."
    assert len(battle.actions) == 1 and battle.model_budget == [0, 1, 0]
    messages = battle.inputs[1]["messages"]
    assert isinstance(messages, list)
    assistant_index = next(
        index
        for index, value in enumerate(messages)
        if isinstance(value, dict) and value.get("role") == "assistant"
    )
    following = messages[assistant_index + 1]
    assert isinstance(following, dict) and following["role"] == "user"
    results = following["content"]
    assert isinstance(results, list) and len(results) == 2
    assert [value["tool_use_id"] for value in results if isinstance(value, dict)] == [
        "first",
        "second",
    ]


def test_messages_empty_reply_is_archived_but_omitted_from_continuation(tmp_path: Path) -> None:
    from sts2_harness.client import models as native
    from sts2_harness.journal import Journal, Manifest
    from sts2_harness.journal_models import AssistantSnapshot
    from sts2_harness.model_journal import RecordedLlm

    empty = events(stop="end_turn")
    responses = iter((sse(empty[:1] + empty[-2:]), sse(events())))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=next(responses), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "empty.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="empty",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
        history = (TextMessage(TextRole.USER, "Inspect the current state."),)
        correction = TextMessage(TextRole.USER, "Continue the current action task.")
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages"), transport=httpx.MockTransport(handler)
        ) as llm:
            recorded = RecordedLlm(
                llm, journal, role=native.DecisionOwner.COMBAT, stream="combat:1"
            )
            reply = await recorded.complete(history, ())
            assert reply.assistant.text == "" and reply.assistant.tool_calls == ()
            assert reply.usage.input_tokens == 100 and reply.usage.output_tokens == 5
            await recorded.complete((*history, reply.assistant, correction), ())
        journal.close()
        archive = Journal.open(journal.path)
        first, second = archive.requests()
        assert first.delivered and first.failure is None
        retained = first.attempts[0].reply
        assert retained is not None and first.attempts[0].failure is None
        raw = json.loads(retained.native_output)
        assert raw["content"] == [] and raw["stop_reason"] == "end_turn"
        assert raw["opaque"] == {"keep": None}
        assert retained.input_tokens == 100 and retained.output_tokens == 5
        saved = second.input[1]
        assert isinstance(saved, AssistantSnapshot)
        assert saved.native_reply.to_json() == retained.native_output
        assert second.delivered and second.base_sequence == first.sequence
        archive.close()
        assert json.loads(requests[1].content)["messages"] == [
            {"role": "user", "content": "Inspect the current state."},
            {"role": "user", "content": "Continue the current action task."},
        ]

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


def test_messages_clean_eof_retries_only_in_existing_llm_budget(tmp_path: Path) -> None:
    replies = iter([sse(events()[:-1]), sse(events())])
    waits: list[float] = []
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200, content=next(replies), headers={"content-type": "text/event-stream"}
        )

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages", LLM_EXTRA_RETRIES="1"),
            transport=httpx.MockTransport(handler),
            sleep=sleep,
        ) as llm:
            reply = await llm.complete((), ())
            assert reply.assistant.tool_calls[0].id == "call_1"
        assert requests == 2 and waits == [2]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutation",
    [
        "partial-json",
        "duplicate-json",
        "after-stop",
        "wrong-index",
        "missing-stop",
        "wrong-delta",
        "duplicate-start",
        "unknown-block",
        "wrong-type",
    ],
)
def test_messages_malformed_stream_is_not_a_deliverable_reply(
    tmp_path: Path, mutation: str
) -> None:
    stream = events()
    if mutation == "partial-json":
        stream[9] = {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '"c1"'},
        }
    elif mutation == "duplicate-json":
        stream[8] = {
            "type": "content_block_delta",
            "index": 2,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"reference":"old","reference":',
            },
        }
    elif mutation == "wrong-index":
        stream[2] = {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "thinking_delta", "thinking": "bad"},
        }
    elif mutation == "missing-stop":
        del stream[10]
    elif mutation == "wrong-delta":
        stream[2] = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "bad"},
        }
    elif mutation == "duplicate-start":
        stream.insert(1, stream[0])
    elif mutation == "unknown-block":
        stream[1] = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "unknown"},
        }
    elif mutation == "after-stop":
        stream.append({"type": "message_stop"})
    else:
        stream[1] = {
            "type": "content_block_start",
            "index": False,
            "content_block": {"type": "thinking", "thinking": 1, "signature": ""},
        }

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=sse(stream), headers={"content-type": "text/event-stream"}
            )

        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages", LLM_EXTRA_RETRIES="0"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is FailureKind.PROTOCOL

    asyncio.run(scenario())


def test_messages_wire_authentication_blocks_ambient_and_honors_custom_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-token")
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        "X-Ambient: ambient-header\nAuthorization: ambient-bearer\nX-Api-Key: ambient-key",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=sse(events()), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        for settings, authorization, key in [
            ({"LLM_AUTH": "none"}, None, None),
            ({"LLM_AUTH": "bearer", "LLM_API_KEY": "configured"}, "Bearer configured", None),
            ({"LLM_AUTH": "api-key", "LLM_API_KEY": "configured"}, None, "configured"),
            (
                {"LLM_AUTH": "basic", "LLM_USERNAME": "user", "LLM_PASSWORD": "pass"},
                "Basic dXNlcjpwYXNz",
                None,
            ),
            (
                {
                    "LLM_AUTH": "bearer",
                    "LLM_API_KEY": "configured",
                    "LLM_HEADERS": "aUtHoRiZaTiOn: custom",
                },
                "custom",
                None,
            ),
            (
                {
                    "LLM_AUTH": "api-key",
                    "LLM_API_KEY": "configured",
                    "LLM_HEADERS": "X-API-KEY: custom",
                },
                None,
                "custom",
            ),
        ]:
            async with Llm(
                configuration(tmp_path, LLM_PROTOCOL="messages", **settings),
                transport=httpx.MockTransport(handler),
            ) as llm:
                await llm.complete((), ())
            headers = requests[-1].headers
            assert headers.get("authorization") == authorization and headers.get("x-api-key") == key
            assert "x-ambient" not in headers and "ambient" not in str(headers)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (401, "authentication_error", FailureKind.AUTHENTICATION),
        (403, "permission_error", FailureKind.PERMISSION),
        (404, "not_found_error", FailureKind.MODEL),
        (400, "invalid_request_error", FailureKind.CONFIGURATION),
        (429, "rate_limit_error", FailureKind.RATE_LIMIT),
        (529, "overloaded_error", FailureKind.OVERLOAD),
        (200, "overloaded_error", FailureKind.OVERLOAD),
        (200, "invalid_request_error", FailureKind.CONFIGURATION),
    ],
)
def test_messages_http_and_stream_errors_are_safe_typed_failures(
    tmp_path: Path, status: int, code: str, reason: FailureKind
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        body: dict[str, object] = {
            "type": "error",
            "error": {"type": code, "message": "private-provider-payload"},
        }
        if status == 200:
            return httpx.Response(
                200, content=sse([body]), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(status, json=body)

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages", LLM_EXTRA_RETRIES="0"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is reason and "private" not in str(failure.value)
        assert requests == 1

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


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (
            {
                "input_tokens": None,
                "output_tokens": None,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
                "service_tier": None,
                "opaque": {"keep": None},
                "opaque_null": None,
            },
            Usage(100, 5, 70, 20),
        ),
        ({"output_tokens": 8}, Usage(100, 8, 70, 20)),
        (
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            Usage(0, 0, 0, 0),
        ),
    ],
    ids=["null", "omitted", "zero"],
)
def test_messages_usage_updates_preserve_known_counts(
    tmp_path: Path, delta: dict[str, object], expected: Usage
) -> None:
    stream = events()
    stream[-2]["usage"] = {
        "output_tokens": 5,
        "service_tier": "standard",
        "opaque_null": {"previous": "value"},
    }
    stream.insert(
        -1,
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": delta},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse(stream), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages"), transport=httpx.MockTransport(handler)
        ) as llm:
            reply = await llm.complete((), ())
            assert reply.usage == expected
            assert reply.metadata.response_service_tier == "standard"
            raw = json.loads(reply.assistant.native.to_json())
            assert raw["opaque"] == {"keep": None}
            if "opaque" in delta:
                assert raw["usage"]["opaque"] == {"keep": None}
                assert raw["usage"]["opaque_null"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "settings", [{"LLM_REASONING_EFFORT": "unsupported"}, {"LLM_SERVICE_TIER": "priority"}]
)
def test_messages_unsupported_options_fail_before_wire(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsupported options reached provider")

    with pytest.raises(ModelUnavailable) as failure:
        Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages", **settings),
            transport=httpx.MockTransport(handler),
        )
    assert failure.value.reason is FailureKind.CONFIGURATION


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


def test_messages_text_preserves_provider_fields_without_sdk_synthesis(tmp_path: Path) -> None:
    stream = events()[:1] + [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "text",
                "text": "",
                "citations": None,
                "parsed_output": {"provider": None},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Visible."},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]

    async def scenario() -> None:
        from sts2_harness.llm.messages import AssistantMessage, ResponsesReply
        from sts2_harness.llm.messages_native import MessagesReply

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=sse(stream), headers={"content-type": "text/event-stream"}
            )

        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages"), transport=httpx.MockTransport(handler)
        ) as llm:
            reply = await llm.complete((), ())
            assert reply.assistant.text == "Visible." and reply.assistant.tool_calls == ()
            native = reply.assistant.native
            assert isinstance(native, MessagesReply)
            decoded = native.message.content[0]
            assert decoded.type == "text"
            decoded.text = "Changed copy."
            assert json.loads(native.to_json())["content"] == [
                {
                    "type": "text",
                    "text": "Visible.",
                    "citations": None,
                    "parsed_output": {"provider": None},
                }
            ]
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((AssistantMessage(ResponsesReply.from_json("[]")),), ())
            assert failure.value.reason is FailureKind.CONFIGURATION
        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(handler)) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((reply.assistant,), ())
            assert failure.value.reason is FailureKind.CONFIGURATION

    asyncio.run(scenario())


def test_messages_explicit_truncation_is_not_retried_when_eof_follows(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            content=sse(events(stop="max_tokens")[:-1]),
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages"), transport=httpx.MockTransport(handler)
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is FailureKind.TRUNCATED
        assert requests == 1

    asyncio.run(scenario())


def test_messages_attempt_timeout_is_owned_by_llm(tmp_path: Path) -> None:
    from collections.abc import AsyncIterator

    requests = 0
    waits: list[float] = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(0.1)
            yield sse(events())

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                200, stream=SlowStream(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200, content=sse(events()), headers={"content-type": "text/event-stream"}
        )

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    async def scenario() -> None:
        async with Llm(
            configuration(
                tmp_path,
                LLM_PROTOCOL="messages",
                LLM_REQUEST_TIMEOUT_S=".05",
                LLM_EXTRA_RETRIES="1",
            ),
            transport=httpx.MockTransport(handler),
            sleep=sleep,
        ) as llm:
            reply = await llm.complete((), ())
            assert reply.assistant.tool_calls[0].id == "call_1"
        assert requests == 2 and waits == [2]

    asyncio.run(scenario())


def test_messages_http_overload_status_survives_malformed_error_details(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(529, json={"error": "malformed details"})

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages", LLM_EXTRA_RETRIES="0"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is FailureKind.OVERLOAD

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("ending", "reason", "complete"),
    [
        ("close-failure", FailureKind.TIMEOUT, True),
        ("after-stop", FailureKind.PROTOCOL, True),
        ("incomplete-eof", FailureKind.INCOMPLETE_EOF, False),
    ],
)
def test_messages_arrival_is_durable_without_delivering_a_failed_stream(
    tmp_path: Path, ending: str, reason: FailureKind, complete: bool
) -> None:
    from collections.abc import AsyncIterator

    from sts2_harness.client import models as native
    from sts2_harness.journal import Journal, Manifest
    from sts2_harness.model_journal import RecordedLlm

    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "arrival.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="arrival",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
        arrived_before_close = False

        class ArrivalStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                stream = events()
                if ending == "after-stop":
                    stream.append({"type": "message_stop"})
                elif ending == "incomplete-eof":
                    stream.pop()
                yield sse(stream)

            async def aclose(self) -> None:
                nonlocal arrived_before_close
                reader = Journal.open(journal.path)
                arrived_before_close = reader.request(1).attempts[0].reply is not None
                reader.close()
                if ending == "close-failure":
                    raise httpx.ReadTimeout("Synthetic stream close failure.")

        def provider(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, stream=ArrivalStream(), headers={"content-type": "text/event-stream"}
            )

        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="messages", LLM_EXTRA_RETRIES="0"),
            transport=httpx.MockTransport(provider),
        ) as llm:
            recorded = RecordedLlm(
                llm, journal, role=native.DecisionOwner.COMBAT, stream="combat:1"
            )
            with pytest.raises(ModelUnavailable) as failure:
                await recorded.complete((), ())
        assert failure.value.reason is reason
        assert arrived_before_close is complete
        journal.close()
        reader = Journal.open(journal.path)
        record = reader.request(1)
        assert not record.delivered and record.failure is reason
        assert record.attempts[0].failure is reason
        reply = record.attempts[0].reply
        if complete:
            assert reply is not None
            assert json.loads(reply.native_output)["content"][2]["input"] == {"reference": "c1"}
            assert reply.input_tokens == 100 and reply.output_tokens == 5
            assert reply.response_model == "reported" and reply.response_service_tier == "standard"
            assert reader.storage.native_blocks == 1
        else:
            assert reply is None and reader.storage.native_blocks == 0
        reader.close()

    asyncio.run(scenario())
