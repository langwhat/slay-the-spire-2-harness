"""User input values shared by the queue and the roles that receive it."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UserInput:
    id: str
    text: str


class UserInputSource(Protocol):
    def drain(self) -> tuple[UserInput, ...]: ...
