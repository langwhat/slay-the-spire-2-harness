import socket
from pathlib import Path

import pytest
from pydantic import JsonValue

from sts2_harness.capacity import CapacityPolicy, measure
from sts2_harness.config import Protocol


def test_character_estimate_counts_unicode_and_structure_once() -> None:
    body: dict[str, JsonValue] = {"input": ["中😀a"], "tools": []}
    # The skeleton has 25 characters; the decoded value adds three, not eight bytes.
    assert measure(body, Protocol.RESPONSES).estimated_input_tokens == 13


@pytest.mark.parametrize("protocol", list(Protocol))
def test_input_estimate_ignores_transport_settings_but_digest_preserves_them(
    protocol: Protocol,
) -> None:
    field = "input" if protocol is Protocol.RESPONSES else "messages"
    body: dict[str, JsonValue] = {
        field: [{"role": "user", "content": 'Choose "A".\nThen continue.'}],
        "tools": [],
    }
    if protocol is Protocol.MESSAGES:
        body["system"] = [{"type": "text", "text": "Follow the game rules."}]
    original = measure(body, protocol)
    configured = measure({**body, "metadata": {"label": "transport-only " * 100}}, protocol)
    assert configured.estimated_input_tokens == original.estimated_input_tokens
    assert configured.digest != original.digest
    assert configured.encoded_bytes > original.encoded_bytes


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("中文 <|endoftext|> hello", 29),
        ("玩家查看三条路线，比较它们的代价，然后选择预期收益最高的路线。", 32),
        ('Line one: "quoted".\nLine two: "quoted".\n' * 100, 1355),
    ],
)
def test_measure_counts_decoded_multilingual_text_and_literal_special_tokens(
    content: str, expected: int
) -> None:
    body: dict[str, JsonValue] = {
        "messages": [{"role": "user", "content": content}],
        "tools": [],
    }
    assert measure(body, Protocol.CHAT_COMPLETIONS).estimated_input_tokens == expected


def test_input_estimate_includes_opaque_native_continuation() -> None:
    body: dict[str, JsonValue] = {"input": [], "tools": []}
    empty = measure(body, Protocol.RESPONSES)
    body["input"] = [
        {"type": "reasoning", "encrypted_content": "synthetic opaque continuation " * 100}
    ]
    continued = measure(body, Protocol.RESPONSES)
    assert continued.estimated_input_tokens > empty.estimated_input_tokens + 300


def test_measurement_needs_no_network_or_tokenizer_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    body: dict[str, JsonValue] = {"input": [], "tools": []}
    assert measure(body, Protocol.RESPONSES).estimated_input_tokens == 8


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
