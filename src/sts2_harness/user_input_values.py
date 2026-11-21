"""User input values shared by the queue and the roles that receive it."""

from dataclasses import dataclass
from typing import Protocol

VIEWER_PREFIX = "[viewer] "
VIEWER_LINE_SEPARATOR = "\n"
VIEWER_TEXT_LIMIT = 50_000


@dataclass(frozen=True, slots=True)
class UserInput:
    id: str
    text: str

    @property
    def line(self) -> str:
        # A viewer's line breaks stay within their own message.
        return VIEWER_PREFIX + " ".join(self.text.split())


class UserInputSource(Protocol):
    def drain(self) -> tuple[UserInput, ...]: ...
