"""Named reads of the packaged gameplay instructions."""

from importlib.resources import files
from typing import Protocol

from sts2_harness.client.models import Progress
from sts2_harness.material_text import MaterialText, MaterialUnavailable

DECK_STATE_FORMAT_SECTION = "\n## Deck reward fields\n"


def deck_act_prompt(progress: Progress) -> str:
    if progress.act == 1:
        return "deck-act-early"
    if progress.acts > 1 and progress.act == progress.acts:
        return "deck-act-final"
    return "deck-act-mid"


class PromptReader(Protocol):
    def read(self, name: str) -> MaterialText: ...


class PackagedPrompts:
    def read(self, name: str) -> MaterialText:
        try:
            body = files("sts2_harness.prompts").joinpath(name + ".md").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise MaterialUnavailable(
                "The required gameplay instructions could not be read."
            ) from None
        return MaterialText.create("prompt:" + name, body)
