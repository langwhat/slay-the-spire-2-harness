import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from sts2_harness.config import ModelConfiguration, Protocol, load_configuration
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


@dataclass(frozen=True)
class PlainResult:
    text: str

    def to_json(self) -> str:
        return self.text


class InspectArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str
    detail: str | None = None


OUTPUT: list[dict[str, JsonValue]] = [
    {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque",
        "content": None,
        "status": None,
        "opaque": {"token": "keep", "unset": None},
    },
    {
        "type": "message",
        "id": "msg_1",
        "status": "completed",
        "role": "assistant",
        "phase": "commentary",
        "content": [{"type": "output_text", "text": "Checking.", "annotations": []}],
    },
    {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "inspect",
        "arguments": '{"reference":"c1"}',
        "status": "completed",
        "async": True,
        "created_by": "actor_1",
    },
]


def completed() -> bytes:
    return (
        "data: "
        + json.dumps(
            {
                "type": "response.completed",
                "sequence_number": 3,
                "response": {
                    "id": "resp_1",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "reported-model",
                    "output": OUTPUT,
                    "usage": {
                        "input_tokens": 12000,
                        "output_tokens": 20,
                        "total_tokens": 12020,
                        "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 7000},
                    },
                    "service_tier": "default",
                },
            }
        )
        + "\n\n"
    ).encode()


@pytest.mark.parametrize("kind", ["message", "function_call"])
@pytest.mark.parametrize("status", ["in_progress", "incomplete"])
def test_completed_response_rejects_unfinished_consumable_items(
    tmp_path: Path, kind: str, status: str
) -> None:
    payload = json.loads(completed().decode().removeprefix("data: "))
    for item in payload["response"]["output"]:
        if item["type"] == kind:
            item["status"] = status
    content = ("data: " + json.dumps(payload) + "\n\n").encode()
    requests = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})

    async def scenario() -> None:
        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(provider)) as llm:
            with pytest.raises(ModelUnavailable) as failed:
                await llm.complete((), ())
            assert failed.value.reason is FailureKind.PROTOCOL

    asyncio.run(scenario())
    assert requests == 1


def configuration(tmp_path: Path, **overrides: str) -> ModelConfiguration:
    config = load_configuration(
        tmp_path / "absent",
        {},
        {
            "LLM_MODEL": "requested-model",
            "LLM_AUTH": "none",
            "LLM_BASE_URL": "https://example.invalid/proxy/v1",
            **overrides,
        },
    )
    assert config.model is not None
    return config.model


class GatedCompletion(httpx.AsyncByteStream):
    def __init__(self, intermediate: asyncio.Event, release: asyncio.Event) -> None:
        self.intermediate = intermediate
        self.release = release

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield (
            b'data: {"type":"response.function_call_arguments.done","sequence_number":1,'
            b'"item_id":"fc_1","output_index":0,"arguments":"{}"}\n\n'
        )
        self.intermediate.set()
        await self.release.wait()
        yield completed()


def test_complete_and_continue_native_reply_once(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    intermediate = asyncio.Event()
    release = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                stream=GatedCompletion(intermediate, release),
                headers={"Content-Type": "Text/Event-Stream; charset=utf-8"},
            )
        return httpx.Response(
            200, content=completed(), headers={"Content-Type": "text/event-stream"}
        )

    async def scenario() -> None:
        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(handler)) as llm:
            history = (
                TextMessage(TextRole.SYSTEM, "Use available tools."),
                TextMessage(TextRole.USER, "Inspect card c1."),
            )
            tools = (
                ToolDefinition(
                    "inspect", MaterialText.create("inspect", "Inspect a card."), InspectArguments
                ),
            )
            pending = asyncio.create_task(llm.complete(history, tools))
            await intermediate.wait()
            assert not pending.done()
            release.set()
            reply = await pending
            assert reply.assistant.text == "Checking."
            assert reply.assistant.tool_calls[0].id == "call_1"
            assert reply.usage.cache_hit_ratio == 1 / 3
            assert reply.metadata.requested_model == "requested-model"
            assert reply.metadata.response_model == "reported-model"
            retained = reply.assistant.native.to_json()
            assert json.loads(retained) == OUTPUT
            assert isinstance(reply.assistant.native, ResponsesReply)
            for item in reply.assistant.native.items:
                if item.type == "function_call":
                    item.arguments = '{"reference":"changed"}'
                if item.type == "message":
                    item.content.clear()
                if item.type == "reasoning":
                    item.encrypted_content = "changed"
            assert reply.assistant.native.to_json() == retained
            assert reply.assistant.text == "Checking."
            assert reply.assistant.tool_calls[0].arguments == '{"reference":"c1"}'
            restored = AssistantMessage(ResponsesReply.from_json(retained))
            await llm.complete(
                (*history, restored, ToolResultMessage("call_1", PlainResult("card info"))), tools
            )
        wire = json.loads(requests[1].content)
        assert wire["input"][:2] == [
            {"role": "system", "content": "Use available tools."},
            {"role": "user", "content": "Inspect card c1."},
        ]
        assert len(wire["input"]) == 6
        assert wire["input"][2:5] == OUTPUT
        assert wire["input"][5] == {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "card info",
        }
        assert "previous_response_id" not in wire
        assert wire["store"] is False
        assert wire["tools"][0]["strict"] is False
        assert "detail" not in wire["tools"][0]["parameters"]["required"]
        assert requests[0].url.path == "/proxy/v1/responses"

    asyncio.run(scenario())


def test_eof_retries_but_explicit_truncation_does_not(tmp_path: Path) -> None:
    calls = 0
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            content = (
                b'data: {"type":"response.function_call_arguments.done","sequence_number":1,'
                b'"item_id":"fc_1","output_index":0,"arguments":"{}"}\n\n'
            )
        elif calls == 2:
            content = completed()
        else:
            content = (
                b'data: {"type":"response.incomplete","sequence_number":2,'
                b'"response":{"status":"incomplete",'
                b'"incomplete_details":{"reason":"max_output_tokens"}}}\n\n'
            )
        return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path), transport=httpx.MockTransport(handler), sleep=sleep
        ) as llm:
            reply = await llm.complete((), ())
            assert reply.assistant.tool_calls[0].arguments == '{"reference":"c1"}'
            assert calls == 2
            assert waits == [2]
            try:
                await llm.complete((), ())
            except ModelUnavailable as error:
                assert error.reason is FailureKind.TRUNCATED
            else:
                raise AssertionError("Explicit truncation must not return a usable reply")
            assert calls == 3
            assert waits == [2]

    asyncio.run(scenario())


def test_retry_budget_classification_and_attempt_timeout(tmp_path: Path) -> None:
    async def scenario() -> None:
        waits: list[float] = []
        calls = 0

        async def sleep(seconds: float) -> None:
            waits.append(seconds)

        def overloaded(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                503,
                json={"error": {"code": "server_error", "message": "secret"}},
                headers={"Retry-After": "999"},
            )

        async with Llm(
            configuration(tmp_path), transport=httpx.MockTransport(overloaded), sleep=sleep
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is FailureKind.OVERLOAD
            assert "secret" not in str(failure.value)
        assert calls == 26
        assert waits == [2, 4, 8, 16, *([32] * 21)]
        assert sum(waits) == 702

        for status, code, reason, expected_calls in [
            (429, "rate_limit_exceeded", FailureKind.RATE_LIMIT, 2),
            (429, "insufficient_quota", FailureKind.QUOTA, 1),
            (401, "invalid_api_key", FailureKind.AUTHENTICATION, 1),
            (403, "forbidden", FailureKind.PERMISSION, 1),
            (404, "model_not_found", FailureKind.MODEL, 1),
            (400, "unsupported_parameter", FailureKind.CONFIGURATION, 1),
        ]:
            calls = 0

            def error_response(
                request: httpx.Request, status: int = status, code: str = code
            ) -> httpx.Response:
                nonlocal calls
                calls += 1
                return httpx.Response(status, json={"error": {"code": code, "message": "secret"}})

            async with Llm(
                configuration(tmp_path, LLM_EXTRA_RETRIES="1"),
                transport=httpx.MockTransport(error_response),
                sleep=sleep,
            ) as llm:
                with pytest.raises(ModelUnavailable) as failure:
                    await llm.complete((), ())
                assert failure.value.reason is reason
            assert calls == expected_calls

    asyncio.run(scenario())


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, chunk_count: int) -> None:
        self.chunk_count = chunk_count

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.chunk_count):
            await asyncio.sleep(0.02)
            yield b": keepalive\n\n"
        yield completed()


def test_whole_attempt_timeout_resets_and_cancellation_propagates(tmp_path: Path) -> None:
    calls = 0
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            stream=SlowStream(10 if calls == 1 else 3),
            headers={"Content-Type": "text/event-stream"},
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_REQUEST_TIMEOUT_S="0.15"),
            transport=httpx.MockTransport(handler),
            sleep=sleep,
        ) as llm:
            reply = await llm.complete((), ())
            assert reply.assistant.text == "Checking."
            assert calls == 2
            assert waits == [2]
            task = asyncio.create_task(llm.complete((), ()))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert waits == [2]

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", [Protocol.RESPONSES, Protocol.CHAT_COMPLETIONS])
def test_final_wire_authentication_and_custom_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: Protocol
) -> None:
    from test_chat_completions import chat_stream

    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "environment-admin")
    monkeypatch.setenv("OPENAI_ORG_ID", "environment-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "environment-project")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "Authorization: Bearer environment-custom\nX-Api-Key: environment-header",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=completed() if protocol is Protocol.RESPONSES else chat_stream(),
            headers={"Content-Type": "text/event-stream"},
        )

    async def scenario() -> None:
        for settings, authorization, named_key in [
            ({"LLM_AUTH": "none"}, None, None),
            ({"LLM_AUTH": "bearer", "LLM_API_KEY": "configured"}, "Bearer configured", None),
            ({"LLM_AUTH": "api-key", "LLM_API_KEY": "configured"}, None, "configured"),
            (
                {"LLM_AUTH": "basic", "LLM_USERNAME": "user", "LLM_PASSWORD": "pass"},
                "Basic dXNlcjpwYXNz",
                None,
            ),
            ({"LLM_AUTH": "none", "LLM_HEADERS": "AuThOrIzAtIoN: custom"}, "custom", None),
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
                configuration(tmp_path, LLM_PROTOCOL=protocol.value, **settings),
                transport=httpx.MockTransport(handler),
            ) as llm:
                await llm.complete((), ())
            headers = requests[-1].headers
            assert headers.get("authorization") == authorization
            assert headers.get("x-api-key") == named_key
            assert "environment" not in str(headers)
            assert requests[-1].url.path == (
                "/proxy/v1/responses"
                if protocol is Protocol.RESPONSES
                else "/proxy/v1/chat/completions"
            )

    asyncio.run(scenario())


def test_usage_unknowns_aggregate_by_tokens_and_requested_settings(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    samples: list[JsonValue] = [
        None,
        {"input_tokens": 0, "input_tokens_details": {"cached_tokens": 0}},
        {"input_tokens": 100, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 100}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(completed().decode().removeprefix("data: ").strip())
        if len(requests) <= len(samples):
            payload["response"]["usage"] = samples[len(requests) - 1]
        content = ("data: " + json.dumps(payload) + "\n\n").encode()
        return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_REASONING_EFFORT="high", LLM_SERVICE_TIER="flex"),
            transport=httpx.MockTransport(handler),
        ) as llm:
            replies = [await llm.complete((), ()) for _ in range(4)]
        assert replies[0].usage == Usage()
        assert replies[1].usage.cache_hit_ratio is None
        assert replies[2].usage.cache_write_tokens is None
        assert replies[2].usage.cache_hit_ratio == 1
        summary = Usage.summarize_cache(tuple(reply.usage for reply in replies))
        assert summary.request_count == 4
        assert summary.comparable_request_count == 3
        assert summary.cache_hit_ratio == 4100 / 12100
        assert summary.complete_coverage is False
        wire = json.loads(requests[0].content)
        assert wire["reasoning"]["effort"] == "high"
        assert wire["service_tier"] == "flex"
        assert replies[0].metadata.requested_effort == "high"
        assert replies[0].metadata.response_service_tier == "default"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ('{"type":"response.completed","response":null}', FailureKind.PROTOCOL),
        ('{"type":"response.completed","response":{"status":"completed"}}', FailureKind.PROTOCOL),
        (
            '{"type":"response.failed","response":{"status":"failed",'
            '"error":{"code":"server_error","message":"private"}}}',
            FailureKind.OVERLOAD,
        ),
        (
            '{"type":"error","code":"unsupported_parameter","message":"private"}',
            FailureKind.CONFIGURATION,
        ),
    ],
)
def test_terminal_failures_do_not_return_usable_reply(
    tmp_path: Path, payload: str, reason: FailureKind
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=f"data: {payload}\n\n".encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path, LLM_EXTRA_RETRIES="0"), transport=httpx.MockTransport(handler)
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is reason
            assert "private" not in str(failure.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("media_type", ["application/json", "text/html"])
def test_non_sse_success_is_protocol_failure_without_retries(
    tmp_path: Path, media_type: str
) -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b'{"status":"completed","output":[]}',
            headers={"Content-Type": media_type},
        )

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    async def scenario() -> None:
        async with Llm(
            configuration(tmp_path), transport=httpx.MockTransport(handler), sleep=sleep
        ) as llm:
            with pytest.raises(ModelUnavailable) as failure:
                await llm.complete((), ())
            assert failure.value.reason is FailureKind.PROTOCOL
        assert calls == 1
        assert waits == []

    asyncio.run(scenario())
