from pathlib import Path

import pytest
from pydantic import JsonValue

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
