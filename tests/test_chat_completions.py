import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import JsonValue
from test_responses import InspectArguments, PlainResult, configuration

from sts2_harness.llm.chat_native import ChatCompletionsReply
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    AssistantMessage,
    FailureKind,
    ModelUnavailable,
    ResponsesReply,
    TextMessage,
    TextRole,
    ToolDefinition,
    ToolResultMessage,
    Usage,
)
from sts2_harness.material_text import MaterialText


def chat_stream(*, finish: str | None = "tool_calls", usage: bool = True) -> bytes:
    chunks: list[dict[str, object]] = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "opaque",
                        "continuation": {"token": "keep", "unset": None},
                        "parsed": "provider-value",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "inspect",
                                    "arguments": '{"reference":',
                                    "parsed_arguments": "provider-value",
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"c1"}'}}]},
                    "finish_reason": finish,
                }
            ]
        },
    ]
    if usage:
        chunks.append(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 5,
                    "total_tokens": 105,
                    "prompt_tokens_details": {"cached_tokens": 25},
                },
            }
        )
    return (
        "".join(
            "data: "
            + json.dumps(
                {
                    "id": "chat_1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "reported-model",
                    "service_tier": "default",
                    **chunk,
                }
            )
            + "\n\n"
            for chunk in chunks
        ).encode()
        + b"data: [DONE]\n\n"
    )


def test_native_chat_continuation_preserves_opaque_fields_and_tool_schema(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=chat_stream(), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        tools = (
            ToolDefinition(
                "inspect", MaterialText.create("inspect", "Inspect a card."), InspectArguments
            ),
        )
        history = (TextMessage(TextRole.SYSTEM, "Use available tools."),)
        async with Llm(
            configuration(
                tmp_path,
                LLM_PROTOCOL="chat-completions",
                LLM_REASONING_EFFORT="high",
                LLM_SERVICE_TIER="flex",
            ),
            transport=httpx.MockTransport(handler),
        ) as llm:
            reply = await llm.complete(history, tools)
            assert reply.assistant.tool_calls[0].name == "inspect"
            assert reply.assistant.tool_calls[0].arguments == '{"reference":"c1"}'
            assert reply.usage.cache_hit_ratio == 0.25
            assert reply.metadata.response_model == "reported-model"
            assert isinstance(reply.assistant.native, ChatCompletionsReply)
            retained = reply.assistant.native.to_json()
            reply.assistant.native.completion.choices[0].message.content = "Changed copy."
            assert reply.assistant.native.to_json() == retained
            assert reply.assistant.text == ""
            await llm.complete(
                (*history, reply.assistant, ToolResultMessage("call_1", PlainResult("card info"))),
                tools,
            )
        wire = json.loads(requests[1].content)
        assert wire["messages"] == [
            {"role": "system", "content": "Use available tools."},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "opaque",
                "continuation": {"token": "keep", "unset": None},
                "parsed": "provider-value",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "inspect",
                            "arguments": '{"reference":"c1"}',
                            "parsed_arguments": "provider-value",
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "card info"},
        ]
        assert wire["tools"][0]["function"]["strict"] is False
        assert "detail" not in wire["tools"][0]["function"]["parameters"]["required"]
        assert wire["reasoning_effort"] == "high" and wire["service_tier"] == "flex"
        assert requests[0].url.path == "/proxy/v1/chat/completions"

    asyncio.run(scenario())


def test_recorded_chat_round_trip_keeps_protocol_and_native_prefix(tmp_path: Path) -> None:
    from sts2_harness.client import models as native
    from sts2_harness.journal import Journal, Manifest
    from sts2_harness.journal_models import AssistantSnapshot
    from sts2_harness.model_journal import RecordedLlm

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=chat_stream(), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "chat.sqlite",
            Manifest(
                recording_id="chat",
                core_version="test",
                game_instance_id="instance",
                run_id="run",
                initial_notes="",
            ),
        )
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="chat-completions"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            recorded = RecordedLlm(
                llm, journal, role=native.DecisionOwner.COMBAT, stream="combat:1"
            )
            history = (TextMessage(TextRole.USER, "Inspect."),)
            reply = await recorded.complete(history, ())
            await recorded.complete((*history, reply.assistant), ())
        journal.close()
        archive = Journal.open(journal.path)
        first, second = archive.requests()
        assert first.attempts[0].reply is not None
        retained = first.attempts[0].reply
        assert retained.protocol.value == "chat-completions"
        assert isinstance(retained.native_reply, ChatCompletionsReply)
        assert retained.native_reply.completion.choices[0].message.model_extra == {
            "reasoning_content": "opaque",
            "continuation": {"token": "keep", "unset": None},
            "parsed": "provider-value",
        }
        assert second.base_sequence == first.sequence and second.prefix_length == 1
        assistant = second.input[-1]
        assert isinstance(assistant, AssistantSnapshot)
        assert assistant.protocol is retained.protocol
        assert assistant.native_output == retained.native_output
        archive.close()

    asyncio.run(scenario())


def chat_reply(*calls: tuple[str, str, str], text: str | None = None) -> bytes:
    delta: dict[str, object] = {"role": "assistant", "content": text}
    if calls:
        delta["tool_calls"] = [
            {
                "index": index,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            for index, (call_id, name, arguments) in enumerate(calls)
        ]
    return (
        "data: "
        + json.dumps(
            {
                "id": "chat_1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "reported",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": "tool_calls" if calls else "stop"}
                ],
            }
        )
        + "\n\ndata: [DONE]\n\n"
    ).encode()


def test_chat_combat_rejects_all_multiple_calls_then_executes_one(tmp_path: Path) -> None:
    from test_combat import BattleHTTP

    from sts2_harness.config import Protocol
    from sts2_harness.decisions import CombatHandoff

    act = '{"steps":[{"kind":"end_turn"}]}'
    battle = BattleHTTP(
        [
            chat_reply(("first", "combat_act", act), ("second", "combat_act", act)),
            chat_reply(("action", "combat_act", act)),
            chat_reply(text="Review complete."),
        ]
    )
    result = asyncio.run(battle.run(tmp_path, protocol=Protocol.CHAT_COMPLETIONS))
    assert isinstance(result, CombatHandoff) and result.review == "Review complete."
    assert len(battle.actions) == 1 and battle.model_budget == [0, 1, 0]
    messages = battle.inputs[1]["messages"]
    assert isinstance(messages, list)
    assert [
        item["tool_call_id"]
        for item in messages
        if isinstance(item, dict) and item.get("role") == "tool"
    ] == ["first", "second"]


def test_chat_early_eof_retries_and_truncation_never_returns_a_proposal(tmp_path: Path) -> None:
    responses = iter([chat_stream(finish=None), chat_stream(), chat_stream(finish="length")])
    waits: list[float] = []
    requests = 0

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200, content=next(responses), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="chat-completions"),
            transport=httpx.MockTransport(handler),
            sleep=sleep,
        ) as llm:
            reply = await llm.complete((), ())
            assert len(reply.assistant.tool_calls) == 1 and requests == 2
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is FailureKind.TRUNCATED
        assert requests == 3 and waits == [2]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (chat_stream(finish="content_filter"), FailureKind.PROTOCOL),
        (chat_stream(finish="function_call"), FailureKind.PROTOCOL),
        (
            b'data: {"error":{"code":"unsupported_parameter","message":"private"}}\n\n',
            FailureKind.CONFIGURATION,
        ),
        (b'data: {"id":"broken"}\n\n', FailureKind.PROTOCOL),
    ],
)
def test_chat_terminal_failures_are_explicit(
    tmp_path: Path, content: bytes, reason: FailureKind
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="chat-completions", LLM_EXTRA_RETRIES="0"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is reason and "private" not in str(failure.value)

    asyncio.run(scenario())


def test_chat_missing_usage_stays_unknown_and_mixed_history_is_rejected(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=chat_stream(usage=False), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="chat-completions"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            reply = await llm.complete((), ())
            assert reply.usage == Usage()
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((AssistantMessage(ResponsesReply.from_json("[]")),), ())
            assert failure.value.reason is FailureKind.CONFIGURATION
        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(handler)) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((reply.assistant,), ())
            assert failure.value.reason is FailureKind.CONFIGURATION
        assert len(requests) == 1

    asyncio.run(scenario())


def test_chat_does_not_return_complete_arguments_before_terminal_chunk(tmp_path: Path) -> None:
    from collections.abc import AsyncIterator

    intermediate = asyncio.Event()
    release = asyncio.Event()

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield chat_stream(finish=None, usage=False).removesuffix(b"data: [DONE]\n\n")
            intermediate.set()
            await release.wait()
            yield (
                b'data: {"id":"chat_1","object":"chat.completion.chunk","created":1,'
                b'"model":"reported-model","choices":[{"index":0,"delta":{},'
                b'"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'
            )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=GatedStream(), headers={"content-type": "text/event-stream"}
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="chat-completions"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            pending = asyncio.create_task(llm.complete((), ()))
            await intermediate.wait()
            assert not pending.done()
            release.set()
            reply = await pending
            assert reply.assistant.tool_calls[0].arguments == '{"reference":"c1"}'
            assert reply.usage == Usage()

    asyncio.run(scenario())


def fragmented_chat(
    *deltas: dict[str, JsonValue], final_choice: dict[str, JsonValue] | None = None
) -> bytes:
    return (
        b"".join(
            (
                "data: "
                + json.dumps(
                    {
                        "id": "chat_1",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "reported",
                        "choices": [
                            {
                                "index": 0,
                                "delta": delta,
                                "finish_reason": "stop" if index == len(deltas) - 1 else None,
                                **((final_choice or {}) if index == len(deltas) - 1 else {}),
                            }
                        ],
                    }
                )
                + "\n\n"
            ).encode()
            for index, delta in enumerate(deltas)
        )
        + b"data: [DONE]\n\n"
    )


@pytest.mark.parametrize(
    ("first_delta", "second_delta"),
    [
        (
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": "{"},
                    }
                ],
            },
            {"tool_calls": [{"index": 0, "id": "call_1", "function": {"arguments": "}"}}]},
        ),
        (
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": "{"},
                    }
                ],
            },
            {"tool_calls": [{"index": 0, "function": {"name": "inspect", "arguments": "}"}}]},
        ),
        (
            {
                "role": "assistant",
                "content": "First.",
                "reasoning_details": [{"type": "reasoning"}],
            },
            {"content": "Second.", "reasoning_details": [{"type": "reasoning"}]},
        ),
        (
            {
                "role": "assistant",
                "content": "First.",
                "reasoning_details": [{"type": "reasoning"}],
            },
            {"content": "Second.", "reasoning_details": [False]},
        ),
    ],
)
def test_invalid_chat_deltas_close_recorded_request_as_protocol_failure(
    tmp_path: Path, first_delta: dict[str, JsonValue], second_delta: dict[str, JsonValue]
) -> None:
    from sts2_harness.client import models as native
    from sts2_harness.journal import Journal, Manifest
    from sts2_harness.model_journal import RecordedLlm

    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            content=fragmented_chat(first_delta, second_delta),
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "failed.sqlite",
            Manifest(
                recording_id="failed",
                core_version="test",
                game_instance_id="instance",
                run_id="run",
            ),
        )
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="chat-completions"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            recorded = RecordedLlm(
                llm, journal, role=native.DecisionOwner.COMBAT, stream="combat:1"
            )
            with pytest.raises(ModelUnavailable) as failure:
                await recorded.complete((), ())
            assert failure.value.reason is FailureKind.PROTOCOL
        journal.close()
        archive = Journal.open(journal.path)
        try:
            (record,) = archive.requests()
            assert not record.delivered and record.failure is FailureKind.PROTOCOL
            (attempt,) = record.attempts
            assert attempt.failure is FailureKind.PROTOCOL and attempt.reply is None
            assert attempt.protocol_encoding_digest is not None
            assert archive.gaps == () and requests == 1
        finally:
            archive.close()

    asyncio.run(scenario())


def test_chat_preserves_choice_extensions_first_seen_in_terminal_chunk(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=fragmented_chat(
                {"role": "assistant", "content": "Complete."},
                {},
                final_choice={"native_finish_reason": {"reason": "STOP", "opaque": None}},
            ),
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="chat-completions"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            reply = await llm.complete((), ())
            native = json.loads(reply.assistant.native.to_json())
            assert native["choices"][0]["native_finish_reason"] == {
                "reason": "STOP",
                "opaque": None,
            }
            assert reply.assistant.text == "Complete."

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("ending", "reason"),
    [
        ("close-failure", FailureKind.TIMEOUT),
        ("read-failure", FailureKind.TIMEOUT),
        ("late-choice", FailureKind.PROTOCOL),
        ("bad-usage", FailureKind.PROTOCOL),
        ("incomplete-eof", FailureKind.INCOMPLETE_EOF),
    ],
)
def test_chat_complete_arrival_is_durable_before_stream_close(
    tmp_path: Path, ending: str, reason: FailureKind
) -> None:
    from collections.abc import AsyncIterator

    from sts2_harness.client import models as native
    from sts2_harness.journal import Journal, Manifest
    from sts2_harness.journal_models import ReplySnapshot
    from sts2_harness.model_journal import RecordedLlm

    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "arrival.sqlite",
            Manifest(
                recording_id="arrival", core_version="test", game_instance_id="game", run_id="run"
            ),
        )
        arrivals: list[ReplySnapshot | None] = []

        def checkpoint() -> None:
            reader = Journal.open(journal.path)
            arrivals.append(reader.request(1).attempts[0].reply)
            reader.close()

        class ArrivalStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                finish = None if ending == "incomplete-eof" else "tool_calls"
                chunks = chat_stream(finish=finish).split(b"\n\n")
                yield b"\n\n".join(chunks[:2]) + b"\n\n"
                checkpoint()
                if ending == "incomplete-eof":
                    return
                if ending == "read-failure":
                    raise httpx.ReadTimeout("Synthetic read failure.")
                tail = json.loads(chunks[2].removeprefix(b"data: "))
                tail.update(service_tier="flex", tail_opaque={"keep": None})
                yield f"data: {json.dumps(tail)}\n\n".encode()
                if ending == "late-choice":
                    yield chunks[1] + b"\n\n"
                elif ending == "bad-usage":
                    tail["usage"]["prompt_tokens"] = -1
                    yield f"data: {json.dumps(tail)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            async def aclose(self) -> None:
                checkpoint()
                if ending == "close-failure":
                    raise httpx.ReadTimeout("Synthetic close failure.")

        def provider(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, stream=ArrivalStream(), headers={"content-type": "text/event-stream"}
            )

        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL="chat-completions", LLM_EXTRA_RETRIES="0"),
            transport=httpx.MockTransport(provider),
        ) as llm:
            recorded = RecordedLlm(llm, journal, role=native.DecisionOwner.RUN, stream="run")
            with pytest.raises(ModelUnavailable) as failure:
                await recorded.complete((), ())
        assert failure.value.reason is reason
        first, last = arrivals[0], arrivals[-1]
        if ending == "incomplete-eof":
            assert first is None and last is None
        else:
            assert first is not None and first.input_tokens is None
            assert last is not None
            if ending == "read-failure":
                assert last == first and last.input_tokens is None
            else:
                assert last.input_tokens == 100 and last.output_tokens == 5
                assert last.cache_read_tokens == 25 and last.response_service_tier == "flex"
                assert json.loads(last.native_output)["tail_opaque"] == {"keep": None}
            assert json.loads(last.native_output)["choices"][0]["message"]["tool_calls"][0] == {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "inspect",
                    "arguments": '{"reference":"c1"}',
                    "parsed_arguments": "provider-value",
                },
            }
        journal.close()
        reader = Journal.open(journal.path)
        record = reader.request(1)
        assert not record.delivered and record.failure is reason
        assert len(record.attempts) == 1 and record.attempts[0].failure is reason
        assert record.attempts[0].reply == last and reader.gaps == ()
        reader.close()

    asyncio.run(scenario())
