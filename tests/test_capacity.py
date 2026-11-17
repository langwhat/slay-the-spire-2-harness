from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from sts2_harness.capacity import CapacityPolicy, measure
from sts2_harness.config import Protocol


def test_input_estimate_includes_opaque_native_continuation() -> None:
    body: dict[str, JsonValue] = {"input": [], "tools": []}
    empty = measure(body, Protocol.RESPONSES)
    body["input"] = [
        {"type": "reasoning", "encrypted_content": "synthetic opaque continuation " * 100}
    ]
    continued = measure(body, Protocol.RESPONSES)
    assert continued.estimated_input_tokens > empty.estimated_input_tokens + 300


def test_reported_usage_carries_forward_without_counting_the_same_assistant_reply_twice() -> None:
    tool_result: JsonValue = TypeAdapter(JsonValue).validate_python(
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "The query result is added now.",
        }
    )
    previous_body = TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "input": [{"role": "user", "content": "Prior request"}],
            "tools": [{"type": "function", "name": "inspect", "parameters": {}}],
        }
    )
    current_body = TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "input": [
                {"role": "user", "content": "Prior request"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "already counted as output"}],
                },
                tool_result,
            ],
            "tools": [{"type": "function", "name": "inspect", "parameters": {}}],
        }
    )
    assistant_body = TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "input": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "already counted as output"}],
                }
            ],
            "tools": [],
        }
    )
    measured = measure(
        current_body,
        Protocol.RESPONSES,
        previous_body=previous_body,
        assistant_body=assistant_body,
        input_tokens=300,
        output_tokens=40,
    )

    assert measured.estimated_input_tokens == 364


def test_reported_usage_falls_back_to_full_utf8_estimate_without_complete_usage() -> None:
    body: dict[str, JsonValue] = {"input": [{"content": "雪"}], "tools": []}

    measured = measure(
        body,
        Protocol.RESPONSES,
        previous_body={"input": [], "tools": []},
        assistant_body={"input": [], "tools": []},
        input_tokens=20,
        output_tokens=None,
    )

    assert measured.estimated_input_tokens == 10


def test_prepared_request_is_sent_unchanged_through_retry(tmp_path: Path) -> None:
    import asyncio
    import json
    from dataclasses import replace

    import httpx2 as httpx
    from test_responses import completed, configuration

    from sts2_harness.llm.client import Llm
    from sts2_harness.llm.messages import TextMessage, TextRole

    requests: list[dict[str, JsonValue]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(503, json={"error": {"code": "unavailable"}})
        return httpx.Response(
            200, content=completed(), headers={"Content-Type": "text/event-stream"}
        )

    async def sleep(_: float) -> None:
        return None

    async def scenario() -> None:
        config = replace(configuration(tmp_path), extra_retries=1)
        async with Llm(config, transport=httpx.MockTransport(provider), sleep=sleep) as llm:
            prepared = llm.prepare((TextMessage(TextRole.USER, "Synthetic 中文"),), ())
            await llm.complete_prepared(prepared)
            assert len(requests) == 2 and requests[0] == requests[1]
            assert prepared.measurement == measure(requests[0], config.protocol)

    asyncio.run(scenario())


def test_fake_http_usage_anchor_counts_only_new_request_messages(tmp_path: Path) -> None:
    import asyncio
    import json
    from dataclasses import replace

    import httpx2 as httpx
    from test_responses import completed, configuration

    from sts2_harness.llm.client import Llm
    from sts2_harness.llm.messages import ModelUsageAnchor, TextMessage, TextRole

    bodies: list[dict[str, JsonValue]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        bodies.append(
            TypeAdapter(dict[str, JsonValue]).validate_python(json.loads(request.content))
        )
        return httpx.Response(
            200, content=completed(), headers={"Content-Type": "text/event-stream"}
        )

    async def scenario() -> None:
        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(provider),
        ) as model:
            prior = (TextMessage(TextRole.USER, "Initial prompt"),)
            prepared = model.prepare(prior, ())
            reply = await model.complete_prepared(prepared)
            new_message = TextMessage(TextRole.USER, "New tool result: 雪")
            current = (*prior, reply.assistant, new_message)
            anchor = ModelUsageAnchor(prepared.body, reply.assistant, reply.usage)
            next_prepared = model.prepare(current, (), usage_anchor=anchor)
            await model.complete_prepared(next_prepared)

        previous_input = bodies[0].get("input")
        next_input = bodies[1].get("input")
        assert isinstance(previous_input, list) and isinstance(next_input, list)
        assert next_input[: len(previous_input)] == previous_input
        assistant_items = json.loads(reply.assistant.native.to_json())
        assert next_input[len(previous_input) : -1] == assistant_items
        new_message = next_input[-1]
        delta = (
            len(
                json.dumps(
                    new_message,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            + 1
        )
        assert len(bodies) == 2
        assert next_prepared.measurement.estimated_input_tokens == 12020 + (delta + 3) // 4

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", list(Protocol))
def test_recorded_configuration_reconstructs_identical_request_without_local_defaults(
    tmp_path: Path, protocol: Protocol
) -> None:
    import asyncio
    from dataclasses import replace

    from test_responses import configuration

    from sts2_harness.journal_models import ConfigurationSnapshot
    from sts2_harness.llm.client import Llm
    from sts2_harness.llm.messages import TextMessage, TextRole

    async def scenario() -> None:
        original = replace(
            configuration(tmp_path, LLM_PROTOCOL=protocol.value), context_window=42_000
        )
        recorded = ConfigurationSnapshot.of(original, CapacityPolicy(context_window=42_000))
        reconstructed = recorded.for_offline_preparation()
        assert reconstructed.context_window == 42_000
        assert reconstructed.protocol == protocol
        assert reconstructed.authentication.secret == ""
        async with Llm(original) as live, Llm(reconstructed) as replay:
            messages = (TextMessage(TextRole.USER, "Synthetic 中文 <|endoftext|>"),)
            assert live.prepare(messages, ()) == replay.prepare(messages, ())

    asyncio.run(scenario())
