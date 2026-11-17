"""Select model settings independently of the native decision owner."""

from enum import StrEnum

from sts2_harness.client.models import DecisionOwner


class ModelPurpose(StrEnum):
    RUN = "run"
    DECK = "deck"
    COMBAT = "combat"

    @property
    def owner(self) -> DecisionOwner:
        if self in (ModelPurpose.RUN, ModelPurpose.DECK):
            return DecisionOwner.RUN
        return DecisionOwner.COMBAT
