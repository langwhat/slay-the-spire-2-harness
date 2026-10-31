import shutil
import socket
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import JsonValue

from sts2_harness import tokenizer
from sts2_harness.capacity import measure
from sts2_harness.cli import main
from sts2_harness.config import ConfigurationError, Protocol


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("中文 <|endoftext|> hello", 1112),
        ("玩家查看三条路线，比较它们的代价，然后选择预期收益最高的路线。", 1136),
    ],
)
def test_measure_counts_canonical_multilingual_text_and_literal_special_tokens(
    content: str, expected: int
) -> None:
    body: dict[str, JsonValue] = {
        "messages": [{"role": "user", "content": content}],
        "tools": [],
    }
    assert measure(body, Protocol.CHAT_COMPLETIONS).estimated_input_tokens == expected


@pytest.mark.parametrize("name", ["cl100k_base", "o200k_base"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_tables_fail_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, name: str
) -> None:
    for table in tokenizer.TABLES:
        if table != name:
            shutil.copyfile(
                tokenizer.TABLE_DIRECTORY / f"{table}.tiktoken", tmp_path / f"{table}.tiktoken"
            )
    monkeypatch.setattr(tokenizer, "TABLE_DIRECTORY", tmp_path)
    if damage == "corrupt":
        (tmp_path / f"{name}.tiktoken").write_bytes(b"corrupt")

    def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    tokenizer.encodings.cache_clear()
    try:
        with pytest.raises(ConfigurationError, match="uv run sts2 prepare-tokenizers"):
            measure({"messages": [], "tools": []}, Protocol.CHAT_COMPLETIONS)
    finally:
        tokenizer.encodings.cache_clear()


def test_start_reports_how_to_prepare_missing_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tokenizer, "TABLE_DIRECTORY", tmp_path)
    tokenizer.encodings.cache_clear()
    try:
        result = main(
            [
                "start",
                "--env-file",
                str(tmp_path / "absent.env"),
                "--set",
                "LLM_MODEL=example",
                "--set",
                "LLM_AUTH=none",
            ]
        )
        assert result == 2
        assert "uv run sts2 prepare-tokenizers" in capsys.readouterr().err
    finally:
        tokenizer.encodings.cache_clear()


def test_explicit_preparation_verifies_tables_and_reuses_valid_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tokenizer.TABLE_DIRECTORY
    monkeypatch.setattr(tokenizer, "TABLE_DIRECTORY", tmp_path)
    requested: list[str] = []

    def download(url: str, *, timeout: int) -> BytesIO:
        requested.append(url)
        return BytesIO((source / url.rsplit("/", 1)[-1]).read_bytes())

    monkeypatch.setattr(tokenizer, "urlopen", download)
    assert main(["prepare-tokenizers"]) == 0
    assert len(requested) == 2
    assert main(["prepare-tokenizers"]) == 0
    assert len(requested) == 2

    def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    tokenizer.encodings.cache_clear()
    try:
        assert (
            measure({"messages": [], "tools": []}, Protocol.CHAT_COMPLETIONS).estimated_input_tokens
            == 1031
        )
    finally:
        tokenizer.encodings.cache_clear()


def test_interrupted_table_download_reports_a_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class TruncatedResponse(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise IncompleteRead(b"partial", 10)

    def download(url: str, *, timeout: int) -> BytesIO:
        return TruncatedResponse()

    monkeypatch.setattr(tokenizer, "TABLE_DIRECTORY", tmp_path)
    monkeypatch.setattr(tokenizer, "urlopen", download)
    assert main(["prepare-tokenizers"]) == 2
    assert "Could not prepare tokenizer tables" in capsys.readouterr().err
    assert not tuple(tmp_path.glob("*.tiktoken"))
