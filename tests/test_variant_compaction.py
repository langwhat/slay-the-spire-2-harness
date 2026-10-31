import asyncio
import json
import sqlite3
import zlib
from contextlib import closing
from hashlib import sha256
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
from sts2_harness.journal_models import VariantPrefixOutcome
from sts2_harness.llm.client import Llm
from sts2_harness.material_text import MaterialText
from sts2_harness.materials import FileDocuments
from sts2_harness.prompts import PackagedPrompts
from sts2_harness_eval import ModelAnchor, Reached, Rejected, reach, take_over
from sts2_harness_eval.sample import SampleBudget
from sts2_harness_eval.takeover import TakeoverCompleted, TakeoverUnsupported
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


def combat_history(protocol: Protocol) -> Source:
    source = Source()
    source.states = [source.states[0], *([source.states[1]] * 6), source.states[-1]]
    source.responses = iter(
        [
            response(
                protocol, ("travel", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')
            ),
            response(
                protocol,
                ("turn1" + "historical_" * 700, "combat_act", '{"steps":[{"kind":"end_turn"}]}'),
            ),
            *(
                response(protocol, (f"turn{i}", "combat_act", '{"steps":[{"kind":"end_turn"}]}'))
                for i in range(2, 7)
            ),
        ]
    )
    return source


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


@pytest.mark.parametrize("protocol", list(Protocol))
@pytest.mark.parametrize("longer", [True, False])
def test_variant_owns_compaction_timing_and_reopens(
    tmp_path: Path,
    protocol: Protocol,
    longer: bool,
) -> None:
    async def scenario() -> None:
        short = "Use the current public state."
        long = short + " Think carefully." * 90
        source_rules = Rules(short if longer else long)
        calibration = await source_case(
            tmp_path / "calibration.sqlite",
            start=False,
            source=combat_history(protocol),
            target_position=4,
            prompts=source_rules,
            model_configuration=configuration(tmp_path, LLM_PROTOCOL=protocol.value),
        )
        with closing(Journal.open(calibration.source.archive)) as journal:
            measured = journal.request(calibration.source.anchor.model_sequence)
            assert measured.preparation is not None
            threshold = measured.preparation.estimate_before + (100 if longer else -100)
        config = configuration(
            tmp_path, LLM_PROTOCOL=protocol.value, LLM_CONTEXT_WINDOW=str(threshold * 10 // 9)
        )
        case = await source_case(
            tmp_path / "source.sqlite",
            start=False,
            source=combat_history(protocol),
            target_position=4,
            prompts=source_rules,
            model_configuration=config,
        )
        original = source_rules.read("combat-task-rules")
        variant = MaterialVariant(
            HistoricalStart(),
            (
                MaterialReplacement(
                    original.id, frozenset({original.version}), long if longer else short
                ),
            ),
        )
        responses = iter(
            [
                response(protocol, ("live1", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                response(protocol, ("live2", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )
        restorer = FakeRestorer(tmp_path, case)
        branch = tmp_path / "variant.sqlite"
        async with Llm(
            config,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=next(responses),
                    headers={"Content-Type": "text/event-stream"},
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
                variant=variant,
            )
        assert isinstance(result, TakeoverCompleted)
        assert len(restorer.actions) == 2
        with (
            closing(Journal.open(case.source.archive)) as source,
            closing(Journal.open(branch)) as saved,
        ):
            original_request = source.request(case.source.anchor.model_sequence)
            assert original_request.preparation is not None
            actual = saved.variant_preparations()[-1]
            assert actual.decision is (
                CompactionDecision.COMPACTED if longer else CompactionDecision.UNCHANGED
            )
            assert original_request.preparation.decision is (
                CompactionDecision.UNCHANGED if longer else CompactionDecision.COMPACTED
            )
            assert actual.after is not None
            assert actual.confirmed_operation_cursor == case.source.target_cursor
            assert actual.configuration.capacity_policy.context_window == config.context_window
            assert actual.outcome is VariantPrefixOutcome.ANCHOR_READY
            latest = saved.requests()[-1]
            anchor = ModelAnchor(saved.manifest.recording_id, latest.sequence)
        async with reach(branch, anchor) as result:
            assert isinstance(result, Reached)

    asyncio.run(scenario())


def test_oversized_variant_records_interruption_without_dispatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        # Keep source recording admissible; only the replacement should exceed the window.
        config = configuration(tmp_path, LLM_CONTEXT_WINDOW="20000")
        case = await source_case(
            tmp_path / "source.sqlite", start=False, model_configuration=config
        )
        prompt = PackagedPrompts().read("combat-task-rules")
        variant = MaterialVariant(
            HistoricalStart(),
            (MaterialReplacement(prompt.id, frozenset({prompt.version}), "instruction " * 20000),),
        )
        restorer = FakeRestorer(tmp_path, case)
        branch = tmp_path / "oversized.sqlite"

        def provider(_: httpx.Request) -> httpx.Response:
            raise AssertionError("An unreachable prefix must not call the provider")

        async with Llm(config, transport=httpx.MockTransport(provider)) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(3, 30),
                stop_requested=lambda: False,
                variant=variant,
            )
        assert isinstance(result, TakeoverUnsupported)
        assert restorer.launches == 0 and not restorer.actions
        with closing(Journal.open(branch)) as saved:
            preparations = saved.variant_preparations()
            assert preparations[-1].outcome is VariantPrefixOutcome.INTERRUPTED
            assert preparations[-1].decision is CompactionDecision.MINIMUM_EXCEEDS_WINDOW
            assert preparations[-1].retained_complete_group_id is None
            assert preparations[-1].active_group_id == 1
            assert preparations[-1].source_reply_sequence is None
            assert preparations[-1].after is not None
            with closing(Journal.open(case.source.archive)) as source:
                assert saved.requests() == source.requests(
                    stop=case.source.anchor.model_sequence - 1
                )
            with closing(sqlite3.connect(f"file:{branch}?mode=ro", uri=True)) as connection:
                assert connection.execute("SELECT count(*) FROM model_requests").fetchone() == (0,)
                assert connection.execute("SELECT count(*) FROM model_attempts").fetchone() == (0,)

    asyncio.run(scenario())


def test_variant_folds_verified_source_recovery_without_replaying_rejection(tmp_path: Path) -> None:
    class RecoveringSource(Source):
        calls = 0

        def model(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if self.calls == 5:
                self.inputs.append(json.loads(request.content))
                return httpx.Response(400, json={"error": {"code": "context_too_large"}})
            return super().model(request)

    async def scenario() -> None:
        history = combat_history(Protocol.RESPONSES)
        source = RecoveringSource()
        source.states, source.responses = history.states, history.responses
        config = configuration(
            tmp_path, LLM_BASE_URL="http://test", LLM_CAPACITY_VERIFIED_ENDPOINT="http://test"
        )
        case = await source_case(
            tmp_path / "source.sqlite",
            start=False,
            source=source,
            target_position=6,
            model_configuration=config,
        )
        with closing(Journal.open(case.source.archive)) as archive:
            rejected = next(item for item in archive.requests() if item.capacity_recovery)
            recovery = rejected.capacity_recovery
            assert recovery is not None and recovery.retry_sequence is not None
            assert rejected.configuration.capacity_policy.capacity_classifier_config.enabled_signals
        prompt = PackagedPrompts().read("combat-task-rules")
        branch = tmp_path / "variant.sqlite"
        responses = iter(
            [
                response(
                    Protocol.RESPONSES,
                    (f"live{i}", "combat_act", '{"steps":[{"kind":"end_turn"}]}'),
                )
                for i in range(2)
            ]
        )
        calls = 0

        def provider(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200, content=next(responses), headers={"Content-Type": "text/event-stream"}
            )

        restorer = FakeRestorer(tmp_path, case)
        async with Llm(config, transport=httpx.MockTransport(provider)) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(3, 30),
                stop_requested=lambda: False,
                variant=MaterialVariant(
                    HistoricalStart(),
                    (
                        MaterialReplacement(
                            prompt.id, frozenset({prompt.version}), "Use current public facts."
                        ),
                    ),
                ),
            )
        assert isinstance(result, TakeoverCompleted)
        assert calls == 2 and len(restorer.actions) == 2
        with closing(Journal.open(branch)) as archive:
            evidence = next(
                item
                for item in archive.variant_preparations()
                if item.source_model_sequence == rejected.sequence
            )
            assert evidence.source_reply_sequence == recovery.retry_sequence
            assert evidence.outcome is VariantPrefixOutcome.SOURCE_REPLY
            latest = archive.requests()[-1]
            anchor = ModelAnchor(archive.manifest.recording_id, latest.sequence)
        async with reach(branch, anchor) as reopened:
            assert isinstance(reopened, Reached)

    asyncio.run(scenario())


@pytest.mark.parametrize("digest", [None, "0" * 64], ids=["missing", "wrong"])
def test_variant_rejects_malformed_source_physical_evidence(
    tmp_path: Path, digest: str | None
) -> None:
    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
        restorer = FakeRestorer(tmp_path, case)
        with closing(sqlite3.connect(case.source.archive)) as connection, connection:
            sequence, attempt, old_digest = connection.execute(
                "SELECT sequence,attempt,value FROM model_attempts ORDER BY sequence LIMIT 1"
            ).fetchone()
            content, kind = connection.execute(
                "SELECT content,kind FROM blobs WHERE digest=?", (old_digest,)
            ).fetchone()
            value = json.loads(zlib.decompress(content))
            value["protocol_encoding_digest"] = digest
            raw = json.dumps(value).encode()
            blob = sha256(raw).hexdigest()
            connection.execute("INSERT INTO blobs VALUES (?,?,?)", (blob, zlib.compress(raw), kind))
            connection.execute(
                "UPDATE model_attempts SET value=? WHERE sequence=? AND attempt=?",
                (blob, sequence, attempt),
            )
        prompt = PackagedPrompts().read("combat-task-rules")

        def provider(_: httpx.Request) -> httpx.Response:
            raise AssertionError("Malformed source evidence must not reach the provider")

        branch = tmp_path / "variant.sqlite"
        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(provider)) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(3, 30),
                stop_requested=lambda: False,
                variant=MaterialVariant(
                    HistoricalStart(),
                    (
                        MaterialReplacement(
                            prompt.id, frozenset({prompt.version}), "Use public facts."
                        ),
                    ),
                ),
            )
        assert isinstance(result, TakeoverUnsupported)
        assert not branch.exists()
        assert restorer.launches == 0 and not restorer.actions

    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["estimate", "encoding", "missing"])
def test_reopened_variant_rejects_altered_preparation(tmp_path: Path, corruption: str) -> None:
    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
        prompt = PackagedPrompts().read("combat-task-rules")
        responses = iter(
            [
                response(
                    Protocol.RESPONSES,
                    (
                        f"live{i}",
                        "combat_act",
                        '{"steps":[{"kind":"end_turn"}]}',
                    ),
                )
                for i in range(2)
            ]
        )
        branch = tmp_path / "variant.sqlite"
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                ),
            ),
        ) as model:
            result = await take_over(
                case,
                FakeRestorer(tmp_path, case),
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(3, 30),
                stop_requested=lambda: False,
                variant=MaterialVariant(
                    HistoricalStart(),
                    (
                        MaterialReplacement(
                            prompt.id, frozenset({prompt.version}), "Read current public facts."
                        ),
                    ),
                ),
            )
        assert isinstance(result, TakeoverCompleted)
        with closing(Journal.open(branch)) as saved:
            target = saved.requests()[-1]
            anchor = ModelAnchor(saved.manifest.recording_id, target.sequence)
        with closing(sqlite3.connect(branch)) as connection, connection:
            row = connection.execute(
                "SELECT value FROM variant_preparations WHERE timeline_index=1"
            ).fetchone()
            value = json.loads(row[0])
            if corruption == "missing":
                connection.execute("DELETE FROM variant_preparations WHERE timeline_index=1")
            else:
                if corruption == "estimate":
                    value["estimate_before"] += 1
                    value["estimate_after"] += 1
                else:
                    value["protocol_encoding_digest"] = "0" * 64
                connection.execute(
                    "UPDATE variant_preparations SET value=? WHERE timeline_index=1",
                    (json.dumps(value),),
                )
        async with reach(branch, anchor) as reopened:
            assert isinstance(reopened, Rejected)
            assert reopened.reason.value == (
                "unsupported_history" if corruption == "missing" else "input_mismatch"
            )

    asyncio.run(scenario())
