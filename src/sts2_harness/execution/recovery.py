"""One run's consecutive tool failures, shared by all actual call boundaries."""

from collections.abc import Callable
from dataclasses import dataclass

from sts2_harness.llm.messages import FailureKind
from sts2_harness.results import (
    ActionCompleted,
    ActionStopped,
    ReadCompleted,
    Reason,
    ReasonCode,
    ToolResult,
)


class ToolRecoveryBudget:
    def __init__(self, *, on_exhausted: Callable[[int], None] | None = None) -> None:
        self._failures = 0
        self._on_exhausted = on_exhausted

    @property
    def failures(self) -> int:
        return self._failures

    def record_failure(self) -> bool:
        self._failures += 1
        if self._failures == 4 and self._on_exhausted is not None:
            self._on_exhausted(self._failures)
        return self._failures >= 4

    def reset(self) -> None:
        self._failures = 0

    def record_outcome(self, result: ToolResult) -> bool:
        outcome = result.outcome
        if isinstance(outcome, (ActionCompleted, ReadCompleted)):
            self.reset()
            return False
        if isinstance(outcome, ActionStopped) and outcome.reason.code not in (
            ReasonCode.CONTROL_UNAVAILABLE,
            ReasonCode.INVALID_ARGUMENTS,
        ):
            return False
        return self.record_failure()


@dataclass(frozen=True, slots=True)
class ModelFailure:
    reason: FailureKind


type InterruptionReason = Reason | ModelFailure


@dataclass(frozen=True, slots=True)
class Interrupted:
    reason: InterruptionReason
    pause_required: bool


class DecisionInterrupted(Exception):
    """A decision failure already accounted for by its owning boundary."""

    def __init__(self, reason: InterruptionReason, pause_required: bool) -> None:
        self.reason = reason
        self.pause_required = pause_required
        super().__init__(reason.message if isinstance(reason, Reason) else reason.reason.value)
