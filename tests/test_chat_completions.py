import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
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
                (
                    *history,
                    reply.assistant,
                    ToolResultMessage("call_1", PlainResult("semantic card info"), "card info"),
                ),
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
