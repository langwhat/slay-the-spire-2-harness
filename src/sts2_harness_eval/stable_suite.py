"""Run a fixed, read-only decision evaluation from frozen inputs."""

import argparse
import asyncio
import base64
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Self

import httpx2 as httpx
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError, model_validator

from sts2_harness.client import models as native
from sts2_harness.config import ConfigurationError, ModelConfiguration, Protocol, load_configuration
from sts2_harness.llm.client import Llm
from sts2_harness.material_text import MaterialText, MaterialUnavailable
from sts2_harness.tools import ToolName, unique_fields
from sts2_harness.tools.inputs import (
    CombatActInput,
    RunActInput,
    RunDeckChoice,
    RunNext,
    RunPlan,
    TakeCard,
)
from sts2_harness_eval.decision_cases import (
    CapturedProposal,
    DecisionCase,
    DecisionSample,
    DecisionVerdict,
    ProposalStatus,
)
from sts2_harness_eval.frozen_catalog import FrozenCatalog
from sts2_harness_eval.frozen_documents import FrozenDocuments
from sts2_harness_eval.stable_decisions import EvaluationRole, sample_fresh
from sts2_harness_eval.traffic import AuditingTransport, HttpAttempt, ReceiptUsage

_BASE_ORB_SLOTS = {
    "CHARACTER.IRONCLAD": 0,
    "CHARACTER.SILENT": 0,
    "CHARACTER.REGENT": 0,
    "CHARACTER.NECROBINDER": 0,
    "CHARACTER.DEFECT": 3,
    "CHARACTER.FOOL": 0,
}
_PROPOSAL_INPUTS: dict[ToolName, type[BaseModel]] = {
    ToolName.RUN_ACT: RunActInput,
    ToolName.RUN_DECK_CHOICE: RunDeckChoice,
    ToolName.RUN_PLAN: RunPlan,
    ToolName.RUN_NEXT: RunNext,
    ToolName.COMBAT_ACT: CombatActInput,
}
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class EvaluationConfig(native.WireModel):
    id: native.Nonempty
    effort: native.Nonempty
    prompt_version: native.Nonempty
    prompt_directory: Path | None = None


class CasePlan(native.WireModel):
    id: native.Nonempty
    role: EvaluationRole
    case: Path
    catalog: Path
    documents: Path | None = None
    # Existing plans may name a link snapshot; evaluations deliver no candidate links.
    candidate_links: Path | None = None
    configurations: Annotated[tuple[native.Nonempty, ...], Field(min_length=1)]
    repetitions: Annotated[int, Field(ge=1)]


class HistoricalMean(native.WireModel):
    role: EvaluationRole
    effort: native.Nonempty
    requests_per_trial: Annotated[float, Field(gt=0)]
    weighted_tokens_per_request: Annotated[float, Field(ge=0)]
    source: native.Nonempty


class EvaluationPlan(native.WireModel):
    configurations: Annotated[tuple[EvaluationConfig, ...], Field(min_length=1)]
    cases: Annotated[tuple[CasePlan, ...], Field(min_length=1)]
    historical_means: tuple[HistoricalMean, ...] = ()

    @model_validator(mode="after")
    def distinct_keys(self) -> Self:
        config_ids = [item.id for item in self.configurations]
        case_ids = [item.id for item in self.cases]
        mean_keys = [(item.role, item.effort) for item in self.historical_means]
        if len(set(config_ids)) != len(config_ids) or len(set(case_ids)) != len(case_ids):
            raise ValueError("Configuration and case IDs must be unique.")
        if len(set(mean_keys)) != len(mean_keys):
            raise ValueError("Historical means must have unique role and effort keys.")
        for case in self.cases:
            if len(set(case.configurations)) != len(case.configurations):
                raise ValueError("A case cannot repeat a configuration.")
            if not set(case.configurations).issubset(config_ids):
                raise ValueError("A case refers to an unknown configuration.")
        return self


class Estimate(native.WireModel):
    trials: int
    requests: float | None
    weighted_tokens: float | None
    missing_means: tuple[str, ...]


def estimate(plan: EvaluationPlan) -> Estimate:
    configs = {item.id: item for item in plan.configurations}
    means = {(item.role, item.effort): item for item in plan.historical_means}
    trials = 0
    requests = 0.0
    weighted = 0.0
    missing: set[str] = set()
    for case in plan.cases:
        for config_id in case.configurations:
            trials += case.repetitions
            effort = configs[config_id].effort
            mean = means.get((case.role, effort))
            if mean is None:
                missing.add(f"{case.role.value}/{effort}")
                continue
            expected_requests = case.repetitions * mean.requests_per_trial
            requests += expected_requests
            weighted += expected_requests * mean.weighted_tokens_per_request
    return Estimate(
        trials=trials,
        requests=None if missing else requests,
        weighted_tokens=None if missing else weighted,
        missing_means=tuple(sorted(missing)),
    )


@dataclass(frozen=True, slots=True)
class FrozenPrompts:
    texts: dict[str, MaterialText]
    digest: str

    def read(self, name: str) -> MaterialText:
        if name not in self.texts:
            raise MaterialUnavailable("The frozen prompt set is missing an instruction.")
        return self.texts[name]


def _prompts(directory: Path | None) -> FrozenPrompts:
    source = files("sts2_harness.prompts") if directory is None else directory
    entries = sorted(
        (entry for entry in source.iterdir() if entry.name.endswith(".md")),
        key=lambda item: item.name,
    )
    texts = {
        entry.name.removesuffix(".md"): MaterialText.create(
            "prompt:" + entry.name.removesuffix(".md"), entry.read_text(encoding="utf-8")
        )
        for entry in entries
    }
    digest = sha256(
        json.dumps({key: value.version for key, value in texts.items()}, sort_keys=True).encode()
    ).hexdigest()
    return FrozenPrompts(texts=texts, digest=digest)


@dataclass(frozen=True, slots=True)
class PreparedCase:
    plan: CasePlan
    case: DecisionCase | None
    catalog: FrozenCatalog
    documents: FrozenDocuments | None
    error: str | None
    hashes: dict[str, str]
    inferred: tuple["InferredValue", ...]


class InferredValue(native.WireModel):
    path: str
    value: int
    basis: str


def infer_old_run_player(value: JsonValue, path: str, inferred: list[InferredValue]) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            infer_old_run_player(item, f"{path}[{index}]", inferred)
        return
    if not isinstance(value, dict):
        return
    state = value.get("state")
    if isinstance(value.get("location"), dict) and isinstance(state, dict):
        player = state.get("player")
        if (
            state.get("phase") == "run"
            and isinstance(player, dict)
            and "base_orb_slots" not in player
        ):
            identity = player.get("definition_id")
            slots = _BASE_ORB_SLOTS.get(identity) if isinstance(identity, str) else None
            if slots is not None:
                player["base_orb_slots"] = slots
                inferred.append(
                    InferredValue(
                        path=f"{path}.state.player.base_orb_slots",
                        value=slots,
                        basis=f"Verified {identity} native default",
                    )
                )
    for key, item in value.items():
        infer_old_run_player(item, f"{path}.{key}", inferred)


def _read(path: Path, root: Path) -> bytes:
    return (root / path).read_bytes()


def prepare_case(plan: CasePlan, root: Path) -> PreparedCase:
    data = _read(plan.case, root)
    catalog = _read(plan.catalog, root)
    documents = _read(plan.documents, root) if plan.documents is not None else None
    hashes = {"case": sha256(data).hexdigest(), "catalog": sha256(catalog).hexdigest()}
    if documents is not None:
        hashes["documents"] = sha256(documents).hexdigest()
    inferred: list[InferredValue] = []
    try:
        recorded = TypeAdapter(dict[str, JsonValue]).validate_json(data)
        infer_old_run_player(recorded, "$", inferred)
        case = DecisionCase.model_validate_json(json.dumps(recorded))
        error = None
    except ValidationError as failure:
        locations = sorted(
            {".".join(str(part) for part in entry["loc"]) for entry in failure.errors()}
        )
        case, error = None, "The recorded schema cannot be reconstructed: " + ", ".join(locations)
    return PreparedCase(
        plan=plan,
        case=case,
        catalog=FrozenCatalog.model_validate_json(catalog),
        documents=FrozenDocuments.model_validate_json(documents) if documents is not None else None,
        error=error,
        hashes=hashes,
        inferred=tuple(inferred),
    )


class Attempt(native.WireModel):
    started_at: str
    status: int | None
    error: str | None
    terminal_event: str | None
    completed_seconds: float | None
    closed_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    weighted_tokens: float | None
    request_base64: str
    response_base64: str


def _attempt(item: HttpAttempt) -> Attempt:
    usage = item.usage or ReceiptUsage(None, None, None, None, None)
    return Attempt(
        started_at=item.started_at,
        status=item.status,
        error=item.error,
        terminal_event=item.terminal_event,
        completed_seconds=item.completed_seconds,
        closed_seconds=item.closed_seconds,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        weighted_tokens=usage.weighted_tokens,
        request_base64=base64.b64encode(item.body).decode(),
        response_base64=base64.b64encode(item.raw).decode(),
    )


class Trial(native.WireModel):
    case_id: str
    role: EvaluationRole
    configuration: str
    repetition: int
    prompt_version: str
    prompt_digest: str
    input_hashes: dict[str, str]
    inferred: tuple[InferredValue, ...] = ()
    sample: DecisionSample | None
    error: str | None
    attempts: tuple[Attempt, ...]


def _totals(attempts: tuple[Attempt, ...]) -> tuple[float, int, int, int]:
    weighted = sum(item.weighted_tokens for item in attempts if item.weighted_tokens is not None)
    missing_weighted = sum(item.weighted_tokens is None for item in attempts)
    reasoning = sum(item.reasoning_tokens for item in attempts if item.reasoning_tokens is not None)
    missing_reasoning = sum(item.reasoning_tokens is None for item in attempts)
    return weighted, missing_weighted, reasoning, missing_reasoning


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _flag(value: bool | None) -> str:
    return "unknown" if value is None else "yes" if value else "no"


def _token_total(values: tuple[int | None, ...]) -> str:
    known = sum(value for value in values if isinstance(value, int))
    missing = sum(value is None for value in values)
    return f"{known} ({missing} unknown)"


def _proposal_action(proposal: CapturedProposal) -> tuple[ToolName, dict[str, JsonValue]] | None:
    schema = _PROPOSAL_INPUTS.get(proposal.tool)
    if schema is None:
        return None
    try:
        json.loads(proposal.arguments, object_pairs_hook=unique_fields)
        parsed = schema.model_validate_json(proposal.arguments, strict=True)
        value = (
            parsed.root
            if isinstance(parsed, (CombatActInput, RunActInput, RunDeckChoice))
            else parsed
        )
        action = _JSON_OBJECT.validate_json(value.model_dump_json(exclude={"reason"}), strict=True)
    except (ValueError, ValidationError):
        return None
    if isinstance(value, TakeCard):
        action["card"] = list(value.cards)
    return proposal.tool, action


def report(plan: EvaluationPlan, trials: tuple[Trial, ...]) -> str:
    expected = estimate(plan)
    all_attempts = tuple(attempt for trial in trials for attempt in trial.attempts)
    weighted, missing_weighted, _, _ = _totals(all_attempts)
    verdicts = Counter(
        trial.sample.verdict if trial.sample is not None else DecisionVerdict.UNSUPPORTED
        for trial in trials
    )
    passed, failed = verdicts[DecisionVerdict.PASSED], verdicts[DecisionVerdict.FAILED]
    rows = [
        "# Decision evaluation",
        "",
        f"Estimated: {expected.trials} trials, {expected.requests} requests, "
        f"{expected.weighted_tokens} weighted tokens. "
        f"Missing historical means: {', '.join(expected.missing_means) or 'none'}.",
        f"Actual: {len(all_attempts)} requests, {weighted:g} known weighted tokens "
        f"({missing_weighted} requests with unknown weighted usage).",
        f"Input: {_token_total(tuple(a.input_tokens for a in all_attempts))}; "
        f"output: {_token_total(tuple(a.output_tokens for a in all_attempts))}; "
        f"cache read: {_token_total(tuple(a.cache_read_tokens for a in all_attempts))}; "
        f"cache write: {_token_total(tuple(a.cache_write_tokens for a in all_attempts))}.",
        f"Scored: {passed + failed}; passed: {passed}; failed: {failed}. "
        f"Unlabeled: {verdicts[DecisionVerdict.UNLABELED]}; "
        f"unsupported: {verdicts[DecisionVerdict.UNSUPPORTED]}; "
        f"deviated: {verdicts[DecisionVerdict.DEVIATED]}.",
        "Combat trials add frozen current-combat history to the initial task; "
        "the production task does not include this additional field.",
        "",
        "| Case | Role | Configuration | Trial | Verdict | Choice | Proposal | "
        "Proposal status | Reason | Input | Output | Cache read | Reasoning | Weighted |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | ---: | "
        "---: | ---: | ---: | ---: |",
    ]
    for trial in trials:
        sample = trial.sample
        usage, unknown_usage, reasoning, unknown_reasoning = _totals(trial.attempts)
        reason = sample.reason if sample is not None else trial.error or ""
        choices = ", ".join(sample.choices) if sample is not None else ""
        proposal = sample.proposal.to_json() if sample is not None and sample.proposal else ""
        proposal_status = (
            sample.proposal_status.value if sample is not None and sample.proposal_status else ""
        )
        verdict = sample.verdict.value if sample is not None else DecisionVerdict.UNSUPPORTED.value
        if sample is not None and sample.error:
            reason = (reason + " " + sample.error).strip()
        rows.append(
            f"| {_escape(trial.case_id)} | {trial.role.value} | {_escape(trial.configuration)} | "
            f"{trial.repetition} | {verdict} | {_escape(choices)} | {_escape(proposal)} | "
            f"{proposal_status} | "
            f"{_escape(reason)} | "
            f"{_token_total(tuple(a.input_tokens for a in trial.attempts))} | "
            f"{_token_total(tuple(a.output_tokens for a in trial.attempts))} | "
            f"{_token_total(tuple(a.cache_read_tokens for a in trial.attempts))} | "
            f"{reasoning} ({unknown_reasoning} unknown) | {usage:g} ({unknown_usage} unknown) |"
        )
    rows.extend(
        (
            "",
            "## Route metrics",
            "",
            "Low HP is at most 40% of maximum HP. Forced Elite considers all ordinary map "
            "continuations from the first planned room until a rest site or Boss; future "
            "special movement is not modeled. These metrics do not score a trial.",
            "",
        )
    )
    route_count = 0
    for trial in trials:
        sample = trial.sample
        if (
            sample is None
            or sample.proposal is None
            or sample.proposal.tool is not ToolName.RUN_PLAN
        ):
            continue
        route_count += 1
        label = f"{_escape(trial.case_id)}: {_escape(trial.configuration)} #{trial.repetition}"
        metrics = sample.route_metrics
        if metrics is None:
            rows.append(f"- {label}: route metrics unknown.")
            continue
        rows.append(
            f"- {label}: {'complete' if metrics.full_route else 'partial'} route; "
            f"{metrics.planned_elites} planned Elites; HP {metrics.hp}/{metrics.max_hp}; "
            f"low HP {_flag(metrics.low_hp)}; "
            f"forced Elite before recovery {_flag(metrics.forced_elite_before_recovery)}; "
            f"low-HP forced Elite {_flag(metrics.bad_low_hp_route)}."
        )
    if not route_count:
        rows.append("No route proposals.")
    rows.extend(("", "## Inferred historical values", ""))
    recorded_inferences: set[tuple[str, str]] = set()
    for trial in trials:
        for item in trial.inferred:
            key = (trial.case_id, item.path)
            if key in recorded_inferences:
                continue
            recorded_inferences.add(key)
            rows.append(
                f"- {_escape(trial.case_id)}: `{_escape(item.path)}` = {item.value}; {item.basis}."
            )
    if not recorded_inferences:
        rows.append("None.")
    rows.extend(("", "## Differences between configurations", ""))
    differences = 0
    incomparable = False
    for index, left in enumerate(trials):
        for right in trials[index + 1 :]:
            if left.case_id != right.case_id or left.configuration == right.configuration:
                continue
            first, second = left.sample, right.sample
            if first is None or second is None:
                incomparable = True
                continue
            first_action: tuple[ToolName, dict[str, JsonValue]] | None = None
            second_action: tuple[ToolName, dict[str, JsonValue]] | None = None
            if any(
                sample.proposal is not None
                and sample.proposal.tool is ToolName.RUN_PLAN
                and sample.proposal_status is ProposalStatus.INVALID
                for sample in (first, second)
            ):
                incomparable = True
                continue
            if first.proposal is not None and first.proposal.tool is ToolName.RUN_PLAN:
                first_action = _proposal_action(first.proposal)
                if first_action is None:
                    incomparable = True
                    continue
            if second.proposal is not None and second.proposal.tool is ToolName.RUN_PLAN:
                second_action = _proposal_action(second.proposal)
                if second_action is None:
                    incomparable = True
                    continue
            if first_action is not None and first_action == second_action:
                continue
            if first.choices and second.choices:
                if first.choices != second.choices:
                    differences += 1
                    rows.append(
                        f"- {_escape(left.case_id)}: {_escape(left.configuration)} "
                        f"#{left.repetition} "
                        f"chose {_escape(', '.join(first.choices))}: {_escape(first.reason)}; "
                        f"{_escape(right.configuration)} #{right.repetition} "
                        f"chose {_escape(', '.join(second.choices))}: {_escape(second.reason)}."
                    )
                continue
            if first.proposal is None or second.proposal is None:
                incomparable = True
                continue
            if first_action is None:
                first_action = _proposal_action(first.proposal)
            if second_action is None:
                second_action = _proposal_action(second.proposal)
            if first_action is None or second_action is None:
                incomparable = True
                continue
            if first_action == second_action:
                continue
            differences += 1
            first_status = first.error or first.verdict.value
            second_status = second.error or second.verdict.value
            if first.verdict not in (DecisionVerdict.PASSED, DecisionVerdict.FAILED):
                first_status += "; unscored"
            if second.verdict not in (DecisionVerdict.PASSED, DecisionVerdict.FAILED):
                second_status += "; unscored"
            rows.append(
                f"- {_escape(left.case_id)}: {_escape(left.configuration)} #{left.repetition} "
                f"declared {_escape(first.proposal.to_json())}: {_escape(first.reason)} "
                f"({_escape(first_status)}); "
                f"{_escape(right.configuration)} #{right.repetition} "
                f"declared {_escape(second.proposal.to_json())}: {_escape(second.reason)} "
                f"({_escape(second_status)})."
            )
    if not differences:
        rows.append("No differing comparable choices or proposals observed.")
    if incomparable:
        rows.append(
            "Some trials could not be compared because a choice or proposal is unavailable "
            "or its arguments could not be safely decoded."
        )
    rows.extend(
        (
            "",
            "Unlabeled and unsupported trials are not scored. "
            "Full attempts and timestamps are in the trial JSON files.",
            "",
        )
    )
    return "\n".join(rows)


async def evaluate(
    plan: EvaluationPlan,
    root: Path,
    output: Path,
    configuration: ModelConfiguration,
    *,
    concurrency: int,
    transport: Callable[[], httpx.AsyncBaseTransport] = httpx.AsyncHTTPTransport,
) -> tuple[Trial, ...]:
    if concurrency < 1:
        raise ValueError("Concurrency must be positive.")
    if configuration.protocol is not Protocol.RESPONSES:
        raise ValueError("The stable evaluator currently requires the Responses protocol.")
    cases = tuple(prepare_case(case, root) for case in plan.cases)
    configs = {item.id: item for item in plan.configurations}
    prompts = {
        item.id: _prompts(
            root / item.prompt_directory if item.prompt_directory is not None else None
        )
        for item in plan.configurations
    }
    expected = estimate(plan)
    print(
        f"Estimated trials={expected.trials}, requests={expected.requests}, "
        f"weighted_tokens={expected.weighted_tokens}; missing_means={expected.missing_means}",
        flush=True,
    )
    output.mkdir(parents=True, exist_ok=False)
    output.joinpath("plan.json").write_text(plan.model_dump_json(indent=2) + "\n")
    jobs = [
        (case, configs[config_id], repetition)
        for case in cases
        for config_id in case.plan.configurations
        for repetition in range(1, case.plan.repetitions + 1)
    ]
    semaphore = asyncio.Semaphore(concurrency)
    results: list[Trial | None] = [None] * len(jobs)

    async def run(
        index: int, case: PreparedCase, config: EvaluationConfig, repetition: int
    ) -> None:
        async with semaphore:
            audit = AuditingTransport(transport())
            sample: DecisionSample | None = None
            error = case.error
            if case.case is not None:
                selected = replace(configuration, reasoning_effort=config.effort, extra_retries=0)
                try:
                    async with Llm(selected, transport=audit) as client:
                        sample = await sample_fresh(
                            case.case,
                            client,
                            catalog=case.catalog,
                            role=case.plan.role,
                            sample=repetition,
                            documents=case.documents,
                            prompts=prompts[config.id],
                        )
                except Exception as failure:
                    error = f"Trial failed: {type(failure).__name__}: {failure}"
            trial = Trial(
                case_id=case.plan.id,
                role=case.plan.role,
                configuration=config.id,
                repetition=repetition,
                prompt_version=config.prompt_version,
                prompt_digest=prompts[config.id].digest,
                input_hashes=case.hashes,
                inferred=case.inferred,
                sample=sample,
                error=error,
                attempts=tuple(_attempt(item) for item in audit.attempts),
            )
            results[index] = trial
            output.joinpath(f"trial-{index + 1:04d}.json").write_text(
                trial.model_dump_json(indent=2) + "\n"
            )
            output.joinpath("report.md").write_text(
                report(plan, tuple(item for item in results if item is not None))
            )

    await asyncio.gather(*(run(index, *job) for index, job in enumerate(jobs)))
    return tuple(item for item in results if item is not None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        plan = EvaluationPlan.model_validate_json(args.plan.read_bytes())
        configuration = load_configuration(args.env_file, os.environ, {})
        if configuration.model is None:
            raise ConfigurationError("A builtin model configuration is required.")
        asyncio.run(
            evaluate(
                plan,
                args.plan.parent,
                args.output,
                configuration.model,
                concurrency=args.concurrency,
            )
        )
        return 0
    except (OSError, ValueError, ValidationError, ConfigurationError) as error:
        print(f"Evaluation error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
