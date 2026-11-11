import asyncio
from contextlib import closing
from itertools import chain
from pathlib import Path

import httpx2 as httpx
import pytest
from test_compaction_full_replay import campaign_with_history, response
from test_responses import configuration
from test_takeover import FakeRestorer, Source, source_case

from sts2_harness.capacity import CompactionDecision
from sts2_harness.client import models as native
from sts2_harness.config import Protocol
from sts2_harness.journal import Journal
from sts2_harness.llm.client import Llm
from sts2_harness.material_text import MaterialText
from sts2_harness.materials import FileDocuments
from sts2_harness.prompts import PackagedPrompts
from sts2_harness_eval import ModelAnchor, Reached, reach, take_over
from sts2_harness_eval.sample import SampleBudget
from sts2_harness_eval.takeover import TakeoverCompleted
from sts2_harness_eval.variants import HistoricalStart, MaterialReplacement, MaterialVariant


class Rules:
    def __init__(self, text: str, name: str = "combat-task-rules") -> None:
        self.text = text
        self.name = name

    def read(self, name: str) -> MaterialText:
        original = PackagedPrompts().read(name)
        if name == self.name:
            return MaterialText.create(original.id, self.text)
        return original


def run_history(protocol: Protocol) -> Source:
    campaign = campaign_with_history(protocol)
    source = Source()
    source.states = [*campaign.states[:-1], campaign.states[-2], campaign.states[-1]]
    source.responses = iter(
        chain(
            campaign.responses,
            (response(protocol, ("finish", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),),
        )
    )
    for index, observation in enumerate(source.states):
        data = observation.to_json()
        for old, new in (("c1", "card:1:1"), ("c2", "card:1:2"), ("e1", "enemy:1:1")):
            data = data.replace('"' + old + '"', '"' + new + '"')
        observation = native.Observation.model_validate_json(data)
        observation = observation.model_copy(
            update={
                "state": observation.state.model_copy(
                    update={
                        "progress": observation.state.progress.model_copy(
                            update={"room_type": "monster"}
                        )
                    }
                )
            }
        )
        if isinstance(observation.state, native.CombatState):
            observation = observation.model_copy(
                update={
                    "input_readiness": native.InputReadiness.READY,
                    "state": observation.state.model_copy(
                        update={"screen": native.CombatScreen.COMBAT, "selection": None}
                    ),
                }
            )
        source.states[index] = observation
    return source


@pytest.mark.parametrize("protocol", list(Protocol))
@pytest.mark.parametrize("longer", [True, False], ids=["earlier", "later"])
def test_run_variant_preserves_notes_and_plan_with_own_compaction(
    tmp_path: Path, protocol: Protocol, longer: bool
) -> None:
    async def scenario() -> None:
        short = "Use the current public state."
        long = short + " Think carefully." * 90
        source_rules = Rules(short if longer else long, "run-task-rules")
        calibration = await source_case(
            tmp_path / "calibration.sqlite",
            start=False,
            source=run_history(protocol),
            target_position=1,
            target_stream="combat:2",
            prompts=source_rules,
            model_configuration=configuration(tmp_path, LLM_PROTOCOL=protocol.value),
        )
        with closing(Journal.open(calibration.source.archive)) as journal:
            measured = next(
                item
                for item in journal.requests()
                if item.role is native.DecisionOwner.RUN and item.confirmed_operation_cursor == 3
            )
            assert measured.preparation is not None
            threshold = measured.preparation.estimate_before + (100 if longer else -100)
        config = configuration(
            tmp_path, LLM_PROTOCOL=protocol.value, LLM_CONTEXT_WINDOW=str(threshold * 10 // 9)
        )
        case = await source_case(
            tmp_path / "source.sqlite",
            start=False,
            source=run_history(protocol),
            target_position=1,
            target_stream="combat:2",
            prompts=source_rules,
            model_configuration=config,
        )
        original = source_rules.read("run-task-rules")
        responses = iter(
            [
                response(protocol, (f"live{i}", "combat_act", '{"steps":[{"kind":"end_turn"}]}'))
                for i in range(2)
            ]
        )
        branch = tmp_path / "variant.sqlite"
        restorer = FakeRestorer(tmp_path, case)
        async with Llm(
            config,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                )
            ),
        ) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                source_rules,
                FileDocuments(),
                SampleBudget(3, 30),
                stop_requested=lambda: False,
                variant=MaterialVariant(
                    HistoricalStart(),
                    (
                        MaterialReplacement(
                            original.id, frozenset({original.version}), long if longer else short
                        ),
                    ),
                ),
            )
        assert isinstance(result, TakeoverCompleted)
        assert len(restorer.actions) == 2
        with closing(Journal.open(branch)) as journal:
            actual = next(
                item
                for item in journal.variant_preparations()
                if item.source_model_sequence == measured.sequence
            )
            assert actual.decision is (
                CompactionDecision.COMPACTED if longer else CompactionDecision.UNCHANGED
            )
            assert actual.after is not None
            assembled = actual.after.model_dump_json()
            assert "Opening plan." in assembled and "Updated once." in assembled
            assert "run_plan" in assembled and "run_next" in assembled
            if longer:
                assert actual.baseline is not None
                baseline = actual.baseline.model_dump_json()
                assert "Opening plan." in baseline and "Updated once." not in baseline
            latest = journal.requests()[-1]
            anchor = ModelAnchor(journal.manifest.recording_id, latest.sequence)
        async with reach(branch, anchor) as reopened:
            assert isinstance(reopened, Reached)

    asyncio.run(scenario())
