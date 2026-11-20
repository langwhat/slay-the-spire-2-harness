import asyncio

import pytest
from test_lifecycle import run_state
from test_verification import with_actions

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness_eval import native as replay_native
from sts2_harness_eval.native import _wait_for_result  # pyright: ignore[reportPrivateUsage]
from sts2_harness_eval.verification import Relation, SourceCorrespondence, VerificationFailure


class Controls:
    def __init__(self) -> None:
        self.modes: list[native.ControlIntent] = []

    async def control(self, mode: native.ControlIntent) -> native.ControlReply:
        self.modes.append(mode)
        return native.ControlReply(mode=mode)


def test_ready_state_can_hold_a_transient_result_for_several_reads() -> None:
    expected = with_actions(run_state(1, "node"), True)
    transient = expected.model_copy(update={"game_language": "en"})
    reads = iter((transient, transient, expected))
    correspondence = SourceCorrespondence()

    async def compare() -> native.Observation:
        observed = next(reads)
        correspondence.compare(expected, observed, 12)
        return observed

    controls = Controls()
    client: ModClient = controls  # pyright: ignore[reportAssignmentType]
    result = asyncio.run(_wait_for_result(client, compare))
    assert result is expected
    assert controls.modes == [native.ControlIntent.RUNNING, native.ControlIntent.PAUSED] * 2


def test_matching_result_does_not_have_to_keep_proving_itself() -> None:
    controls = Controls()
    client: ModClient = controls  # pyright: ignore[reportAssignmentType]
    expected = with_actions(run_state(1, "node"), True)

    async def compare() -> native.Observation:
        SourceCorrespondence().compare(expected, expected, 12)
        return expected

    assert asyncio.run(_wait_for_result(client, compare)) is expected
    assert controls.modes == []


def test_permanent_result_difference_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    controls = Controls()
    client: ModClient = controls  # pyright: ignore[reportAssignmentType]
    expected = with_actions(run_state(1, "node"), True)
    actual = expected.model_copy(update={"game_language": "en"})
    monkeypatch.setattr(replay_native, "_RESULT_TIMEOUT_SECONDS", 0)

    async def compare() -> None:
        SourceCorrespondence().compare(expected, actual, 12)

    with pytest.raises(VerificationFailure) as failure:
        asyncio.run(_wait_for_result(client, compare))
    assert failure.value.relation is Relation.SOURCE_REPLAY
    assert failure.value.sequence == 12
    assert failure.value.field_path == "game_language"
