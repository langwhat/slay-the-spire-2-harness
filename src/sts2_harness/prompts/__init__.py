"""Named reads of the packaged gameplay instructions."""

from importlib.resources import files
from typing import Protocol

from sts2_harness.material_text import MaterialText, MaterialUnavailable


class PromptReader(Protocol):
    def read(self, name: str) -> MaterialText: ...


class PackagedPrompts:
    def __init__(self, *, minimal: bool = False) -> None:
        self._minimal = minimal

    def read(self, name: str) -> MaterialText:
        path = name + ".md"
        if self._minimal and name in {
            "combat-tasks",
            "combat-observations",
            "run-observations",
            "state-format",
        }:
            path = "minimal/" + path
        try:
            body = files("sts2_harness.prompts").joinpath(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise MaterialUnavailable(
                "The required gameplay instructions could not be read."
            ) from None
        return MaterialText.create("prompt:" + name, body)
