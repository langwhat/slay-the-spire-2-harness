"""Named reads of the packaged gameplay instructions."""

from importlib.resources import files
from typing import Protocol

from sts2_harness.client.models import Progress
from sts2_harness.material_text import MaterialText, MaterialUnavailable

DECK_STATE_FORMAT_SECTION = "\n## Deck reward fields\n"
_STATE_FORMAT_NOTES_SENTENCE = " `notes` shows the strategy notes."
_RUN_OBSERVATIONS_REVIEW_SENTENCE = (
    " Check a Combat review against the report, the deck, and the notes; a suggested card "
    "category does not restrict the solutions."
)
_COMBAT_OBSERVATIONS_RESERVATIONS_SENTENCE = " Run's potion reservations are plans, not rules."
_DECK_BATTLE_REVIEW_SENTENCE = (
    " Treat a battle review, when present, as one fight's sample rather than grounds for a "
    "narrow one-enemy counter."
)


class PromptReader(Protocol):
    def read(self, name: str) -> MaterialText: ...


def deck_act_prompt(progress: Progress) -> str:
    if progress.act == 1:
        return "deck-act-early"
    if progress.acts > 1 and progress.act == progress.acts:
        return "deck-act-final"
    return "deck-act-mid"


def _rewrite(block: MaterialText, body: str) -> MaterialText:
    return block if body == block.body else MaterialText.create(block.id, body)


def _replace_once(body: str, target: str, replacement: str) -> str:
    count = body.count(target)
    if count != 1:
        raise ValueError(f"Expected exactly 1 occurrence of {target!r}, found {count}.")
    return body.replace(target, replacement, 1)


def state_format_prompt(prompts: PromptReader, *, deck_fields: bool, minimal: bool) -> MaterialText:
    block = prompts.read("state-format")
    body = block.body
    if not deck_fields:
        before, separator, _ = body.partition(DECK_STATE_FORMAT_SECTION)
        if not separator:
            raise ValueError(
                f"Expected section {DECK_STATE_FORMAT_SECTION!r} in state-format prompt."
            )
        body = before
    elif minimal:
        body = _replace_once(body, _STATE_FORMAT_NOTES_SENTENCE, "")
    return _rewrite(block, body)


def run_tasks_prompt(prompts: PromptReader, *, minimal: bool) -> MaterialText:
    return prompts.read("run-tasks-minimal" if minimal else "run-tasks")


def run_task_rules_prompt(prompts: PromptReader, *, minimal: bool) -> MaterialText:
    return prompts.read("run-task-rules-minimal" if minimal else "run-task-rules")


def run_report_reader_prompt(prompts: PromptReader, *, minimal: bool) -> MaterialText:
    return prompts.read("run-report-reader-minimal" if minimal else "run-report-reader")


def run_observations_prompt(prompts: PromptReader, *, minimal: bool) -> MaterialText:
    block = prompts.read("run-observations")
    if not minimal:
        return block
    return _rewrite(block, _replace_once(block.body, _RUN_OBSERVATIONS_REVIEW_SENTENCE, ""))


def combat_tasks_prompt(prompts: PromptReader, *, minimal: bool) -> MaterialText:
    return prompts.read("combat-tasks-minimal" if minimal else "combat-tasks")


def combat_observations_prompt(prompts: PromptReader, *, minimal: bool) -> MaterialText:
    block = prompts.read("combat-observations")
    if not minimal:
        return block
    return _rewrite(
        block, _replace_once(block.body, _COMBAT_OBSERVATIONS_RESERVATIONS_SENTENCE, "")
    )


def deck_tasks_prompt(prompts: PromptReader, *, minimal: bool) -> MaterialText:
    return prompts.read("deck-tasks-minimal" if minimal else "deck-tasks")


def deck_prompt(prompts: PromptReader, *, minimal: bool) -> MaterialText:
    block = prompts.read("deck")
    if not minimal:
        return block
    return _rewrite(block, _replace_once(block.body, _DECK_BATTLE_REVIEW_SENTENCE, ""))


class PackagedPrompts:
    def read(self, name: str) -> MaterialText:
        try:
            body = files("sts2_harness.prompts").joinpath(name + ".md").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise MaterialUnavailable(
                "The required gameplay instructions could not be read."
            ) from None
        return MaterialText.create("prompt:" + name, body)
