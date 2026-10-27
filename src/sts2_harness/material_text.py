"""Named immutable text snapshots read from explicitly configured ordinary files."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_serializer


class MaterialText(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    id: str
    version: str
    body: str

    @classmethod
    def create(cls, id: str, body: str) -> MaterialText:
        return cls(id=id, version=sha256(body.encode("utf-8")).hexdigest(), body=body)

    @classmethod
    def read(cls, id: str, path: Path) -> MaterialText:
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise MaterialUnavailable("The configured material could not be read.") from None
        return cls.create(id, body)

    @model_serializer
    def encode(self) -> str:
        return self.body


class MaterialUnavailable(Exception):
    """A configured source failed without exposing its filesystem path."""


@dataclass(frozen=True, slots=True)
class MaterialParts:
    parts: tuple[str | MaterialText, ...]

    @property
    def body(self) -> str:
        return "".join(part.body if isinstance(part, MaterialText) else part for part in self.parts)
