"""External replies expose complete public facts without a delivery ledger."""

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.results import Outcome, QueryData, QueryToolResult, SemanticValue, ToolResult


class RawResult(SemanticValue):
    outcome: Outcome
    location: native.Location | MISSING = MISSING
    state: native.State | None
    objects: native.Directory[native.ObjectEntry] | MISSING = MISSING
    descriptions: native.Directory[str | None] | MISSING = MISSING
    modifier_descriptions: native.Directory[native.ModifierDescriptions] | MISSING = MISSING
    upgrades: native.Directory[native.UpgradePreview] | MISSING = MISSING
    generated: native.Directory[tuple[native.GeneratedCard, ...]] | MISSING = MISSING
    mechanisms: tuple[native.MechanismView, ...] | MISSING = MISSING
    map: native.PublicMap | MISSING = MISSING
    data: QueryData | MISSING = MISSING


def render(result: ToolResult) -> RawResult:
    observation = result.observation
    data = result.data if isinstance(result, QueryToolResult) else MISSING
    if observation is None:
        return RawResult(outcome=result.outcome, state=None, data=data)
    return RawResult(
        outcome=result.outcome,
        location=observation.location,
        state=observation.state,
        objects=observation.objects,
        descriptions=observation.descriptions,
        modifier_descriptions=observation.modifier_descriptions,
        upgrades=observation.upgrades,
        generated=observation.generated,
        mechanisms=observation.mechanisms,
        map=observation.map,
        data=data,
    )
