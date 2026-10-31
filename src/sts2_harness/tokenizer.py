"""Offline, verified encodings used for deterministic input accounting."""

import base64
import tempfile
from functools import lru_cache
from hashlib import sha256
from http.client import HTTPException
from importlib.metadata import version
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import tiktoken

from sts2_harness.config import ConfigurationError

TABLES = {
    "cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
}
TABLE_DIRECTORY = Path.home() / ".cache" / "sts2-harness" / "tokenizers"
_PREPARE_COMMAND = "uv run sts2 prepare-tokenizers"


def prepare() -> None:
    """Explicitly download the two fixed tables and verify them before installation."""
    try:
        TABLE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        for name, expected in TABLES.items():
            path = TABLE_DIRECTORY / f"{name}.tiktoken"
            if path.exists() and sha256(path.read_bytes()).hexdigest() == expected:
                continue
            url = f"https://openaipublic.blob.core.windows.net/encodings/{name}.tiktoken"
            with urlopen(url, timeout=60) as response:
                contents = response.read()
            if sha256(contents).hexdigest() != expected:
                raise ConfigurationError(f"Downloaded tokenizer table {name} failed verification.")
            with tempfile.TemporaryDirectory(dir=TABLE_DIRECTORY) as temporary:
                staged = Path(temporary) / path.name
                staged.write_bytes(contents)
                staged.replace(path)
    except (OSError, URLError, HTTPException) as error:
        raise ConfigurationError("Could not prepare tokenizer tables.") from error


# Patterns from tiktoken 0.12.0, Copyright (c) 2022 OpenAI, Shantanu Jain (MIT).
_CL_PATTERN = (
    r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+|"""
    r""" ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
)
_O_PATTERN = "|".join(
    [
        r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
        r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
        r"""\p{N}{1,3}""",
        r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
        r"""\s*[\r\n]+""",
        r"""\s+(?!\S)""",
        r"""\s+""",
    ]
)


@lru_cache(maxsize=1)
def encodings() -> tuple[tiktoken.Encoding, tiktoken.Encoding]:
    if version("tiktoken") != "0.12.0":
        raise ConfigurationError("The model client requires tiktoken==0.12.0; run uv sync.")
    result: list[tiktoken.Encoding] = []
    for name, expected in TABLES.items():
        try:
            contents = (TABLE_DIRECTORY / f"{name}.tiktoken").read_bytes()
        except OSError as error:
            raise ConfigurationError(
                f"Tokenizer tables are missing or unreadable; run {_PREPARE_COMMAND}."
            ) from error
        if sha256(contents).hexdigest() != expected:
            raise ConfigurationError(
                f"Tokenizer table integrity check failed; run {_PREPARE_COMMAND}."
            )
        ranks: dict[bytes, int] = {}
        for line in contents.splitlines():
            token, rank = line.split()
            ranks[base64.b64decode(token)] = int(rank)
        result.append(
            tiktoken.Encoding(
                name=name,
                pat_str=_CL_PATTERN if name == "cl100k_base" else _O_PATTERN,
                mergeable_ranks=ranks,
                special_tokens={},
            )
        )
    return result[0], result[1]
