import pytest
from test_mod_client import RUN

from sts2_harness.client import models as native
from sts2_harness.combat import _system as combat_system  # pyright: ignore[reportPrivateUsage]
from sts2_harness.deck import deck_context
from sts2_harness.llm.messages import TextMessage, TextRole
from sts2_harness.material_text import MaterialParts, MaterialText
from sts2_harness.prompts import (
    PackagedPrompts,
    _replace_once,  # pyright: ignore[reportPrivateUsage]
    combat_observations_prompt,
    deck_prompt,
    run_observations_prompt,
    state_format_prompt,
)
from sts2_harness.run import _system as run_system  # pyright: ignore[reportPrivateUsage]

_FORBIDDEN_IN_MINIMAL = (
    "handoff",
    "strategy notes",
    "`notes`",
    "Combat review",
    "battle review",
    "potion reservations",
)


def _run_observation(*, act: int) -> native.Observation:
    observation = native.Observation.model_validate_json(RUN)
    assert isinstance(observation.state, native.RunState)
    return observation.model_copy(
        update={
            "state": observation.state.model_copy(
                update={
                    "progress": observation.state.progress.model_copy(
                        update={"act": act, "acts": 3}
                    )
                }
            )
        }
    )


def _deck_system_text(prompts: PackagedPrompts, *, minimal: bool, act: int = 1) -> str:
    context = deck_context(prompts, (), _run_observation(act=act), minimal=minimal)
    first = context.model_input()[0]
    assert isinstance(first, TextMessage) and first.role is TextRole.SYSTEM
    assert isinstance(first.value, MaterialParts)
    return first.value.body


def _assert_clean_formatting(text: str) -> None:
    assert "\n\n\n" not in text
    for line in text.splitlines():
        assert "  " not in line.lstrip(), f"Double space found in line: {line!r}"


@pytest.mark.parametrize("act", [1, 2, 3])
def test_minimal_assembled_system_prompts_have_no_dead_references_or_broken_seams(
    act: int,
) -> None:
    prompts = PackagedPrompts()
    observation = _run_observation(act=act)
    run_text = run_system(
        prompts, observation.state.progress, viewer_input=False, minimal=True
    ).body
    deck_text = _deck_system_text(prompts, minimal=True, act=act)
    combat_text = combat_system(prompts, viewer_input=False, minimal=True).body

    for role_text in (run_text, deck_text, combat_text):
        lower = role_text.casefold()
        for forbidden in _FORBIDDEN_IN_MINIMAL:
            assert forbidden.casefold() not in lower
        _assert_clean_formatting(role_text)

    assert "during battle, or" not in run_text
    assert "Submit no game action during battle or while control is not ready." in run_text
    assert "before and after values" not in combat_text
    assert "last_observed_turn" not in combat_text
    assert "not complete fights" not in run_text
    assert "not complete fights" not in deck_text
    assert "not complete fights" not in combat_text


def test_standard_assembled_system_prompts_preserve_full_role_instructions() -> None:
    prompts = PackagedPrompts()
    observation = _run_observation(act=1)
    run_text = run_system(
        prompts, observation.state.progress, viewer_input=False, minimal=False
    ).body
    deck_text = _deck_system_text(prompts, minimal=False, act=1)
    combat_text = combat_system(prompts, viewer_input=False, minimal=False).body

    for role_text in (run_text, deck_text, combat_text):
        _assert_clean_formatting(role_text)

    assert "handoff" in run_text
    assert "strategy notes" in run_text
    assert "`notes` shows the strategy notes." in run_text
    assert "Combat review" in run_text
    assert "battle review" in run_text

    assert "strategy notes" in deck_text
    assert "Combat review" in deck_text
    assert "battle review" in deck_text

    assert "strategy notes" in combat_text
    assert "potion reservations" in combat_text
    assert "before and after values" in combat_text
    assert "last_observed_turn" in combat_text
    assert prompts.read("report-reader").body in combat_text
    assert prompts.read("combat-review").body in combat_text


def test_replace_once_raises_when_target_is_missing_or_duplicated() -> None:
    assert _replace_once("Alpha beta gamma.", " beta", "") == "Alpha gamma."
    with pytest.raises(ValueError, match="Expected exactly 1 occurrence"):
        _replace_once("Alpha gamma.", " beta", "")
    with pytest.raises(ValueError, match="Expected exactly 1 occurrence"):
        _replace_once("beta and beta", "beta", "")


def test_minimal_prompt_helpers_fail_loudly_when_target_sentence_is_missing() -> None:
    class MissingSentencePrompts:
        def read(self, name: str) -> MaterialText:
            return MaterialText.create("prompt:" + name, "Unrelated prompt body.\n")

    stub = MissingSentencePrompts()
    with pytest.raises(ValueError, match="Expected exactly 1 occurrence"):
        state_format_prompt(stub, deck_fields=True, minimal=True)
    with pytest.raises(ValueError, match="Expected exactly 1 occurrence"):
        run_observations_prompt(stub, minimal=True)
    with pytest.raises(ValueError, match="Expected exactly 1 occurrence"):
        combat_observations_prompt(stub, minimal=True)
    with pytest.raises(ValueError, match="Expected exactly 1 occurrence"):
        deck_prompt(stub, minimal=True)
