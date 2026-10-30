"""Assess one executed sample from bounded public evidence and explicit standards."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import file_digest, sha256
from pathlib import Path

from sts2_harness.client import models as native
from sts2_harness.game import Settle, Settlement
from sts2_harness.journal import Journal, Record, SceneVariant
from sts2_harness_eval._sample_binding import bind_sample
from sts2_harness_eval.annotation_records import CardRewardRubric
from sts2_harness_eval.annotations import AnnotationStore
from sts2_harness_eval.assessment_records import (
    ActionObservation,
    ArchiveIdentity,
    Assessed,
    CaseTargetRef,
    ConfirmedAction,
    EvidenceObservation,
    JudgeFailed,
    JudgmentInput,
    ObservationRef,
    PotionUse,
    Quality,
    RewardChoice,
    SampleAssessment,
    SampleEvidence,
    SampleMetrics,
    SampleProvenance,
    SceneInitialRef,
    Unassessed,
    UnassessedReason,
    Uncertainty,
)
from sts2_harness_eval.assessment_records import (
    AssessmentSelection as AssessmentSelection,
)
from sts2_harness_eval.assessment_records import (
    HumanJudgment as HumanJudgment,
)
from sts2_harness_eval.assessment_records import (
    SemanticJudge as SemanticJudge,
)
from sts2_harness_eval.assessment_records import (
    Unscored as Unscored,
)
from sts2_harness_eval.cases import AfterDecisionsSpec, Case
from sts2_harness_eval.execution_record import (
    BattleEndpoint,
    CompletedExecution,
    DecisionEndpoint,
    ExecutionRecord,
    IncompleteExecution,
    UnsupportedExecution,
    execution_record,
)
from sts2_harness_eval.takeover import TakeoverResult


def archive_identity(path: Path) -> ArchiveIdentity:
    """Bind a caller-closed archive by its actual bytes and recorded identity."""
    with path.open("rb") as source:
        digest = file_digest(source, "sha256").hexdigest()
    with closing(Journal.open(path)) as journal:
        recording_id = journal.manifest.recording_id
    return ArchiveIdentity(recording_id=recording_id, sha256=digest)


def _confirmed(record: Record) -> Settlement | None:
    if not isinstance(record.request, Settle) or record.reply is None:
        return None
    outcome = record.reply.outcome
    if not isinstance(outcome, Settlement) or not isinstance(
        outcome.receipt, native.ReceiptCompleted
    ):
        return None
    if (
        outcome.request.operation_id != record.operation_id
        or outcome.receipt.operation_id != record.operation_id
        or outcome.request.expected_route != record.request.route
        or outcome.request.step != record.request.step
    ):
        raise ValueError("A confirmed action has mismatched request or receipt evidence.")
    return outcome


def _card_ids(observation: native.Observation, refs: tuple[str, ...]) -> tuple[str, ...] | None:
    entries = tuple(observation.objects.get(ref) for ref in refs)
    if any(not isinstance(entry, native.CardEntry) for entry in entries):
        return None
    return tuple(entry.definition_id for entry in entries if isinstance(entry, native.CardEntry))


def _reward(
    before: native.Observation,
    before_ref: CaseTargetRef | SceneInitialRef | ObservationRef,
    after: native.Observation,
    after_ref: ObservationRef,
    action: ConfirmedAction,
) -> RewardChoice | None:
    if native.Location.model_validate(action.route.model_dump()) != before.location:
        return None
    state = before.state
    if not isinstance(state, native.RunState) or state.screen is not native.RunScreen.REWARDS:
        return None
    if not isinstance(after.state, (native.RunState, native.TerminalState)):
        return None
    if isinstance(action.step, native.Select):
        selection = state.selection
        if selection is None:
            return None
        selected = action.step.refs
        if (
            not set(selected) <= set(selection.candidates)
            or not selection.min <= len(selected) <= selection.max
        ):
            return None
        candidate_refs = selection.candidates
    elif isinstance(action.step, native.Interact):
        action_ref = action.step.action
        offered = next(
            (
                offer
                for offer in state.offers
                if offer.kind is native.OfferKind.CARD and action_ref in offer.actions
            ),
            None,
        )
        native_action = next((item for item in state.actions if item.ref == action_ref), None)
        if (
            offered is None
            or native_action is None
            or not native_action.enabled
            or not isinstance(native_action.subject, str)
            or native_action.subject not in offered.contents
        ):
            return None
        candidate_refs = offered.contents
        selected = (native_action.subject,)
    else:
        return None
    candidates = _card_ids(before, candidate_refs)
    chosen = _card_ids(before, selected)
    before_deck = _card_ids(before, state.deck)
    after_deck = _card_ids(after, after.state.deck)
    if candidates is None or chosen is None or before_deck is None or after_deck is None:
        return None
    # Native deck refs bind the addition even when several cards have one definition ID.
    added = tuple(ref for ref in after.state.deck if ref not in state.deck)
    if any(ref not in after.state.deck for ref in state.deck) or _card_ids(after, added) != chosen:
        return None
    if after.state.selection is not None:
        return None
    return RewardChoice(
        candidates=candidates,
        selected=chosen,
        skipped=not chosen,
        before_deck=before_deck,
        after_deck=after_deck,
        before=before_ref,
        after=after_ref,
        operation_id=action.operation_id,
        sequence=action.sequence,
    )


@dataclass(frozen=True)
class _Evidence:
    sample: SampleEvidence
    endpoint: EvidenceObservation | None
    before_actions: tuple[ActionObservation, ...]
    uncertainty: tuple[Uncertainty, ...]


def _evidence(
    records: tuple[Record, ...],
    target: native.Observation | None,
    target_ref: CaseTargetRef | SceneInitialRef | None,
    recording_id: str,
) -> _Evidence:
    actions: list[ConfirmedAction] = []
    uses: list[PotionUse] = []
    seen: dict[str, native.StepRequest] = {}
    before = target
    before_ref: CaseTargetRef | SceneInitialRef | ObservationRef | None = target_ref
    reward: RewardChoice | None = None
    pending: list[ConfirmedAction] = []
    writes_known = True
    uncertain: list[Uncertainty] = []
    before_valid = True
    before_actions: list[ActionObservation] = []
    for record in records:
        outcome = _confirmed(record)
        unknown_write = False
        if isinstance(record.request, Settle):
            settlement = record.reply.outcome if record.reply is not None else None
            if not isinstance(settlement, Settlement) or not isinstance(
                settlement.receipt, (native.ReceiptCompleted, native.ReceiptRejected)
            ):
                writes_known = False
                unknown_write = True
        if outcome is not None and outcome.request.operation_id in seen:
            if seen[outcome.request.operation_id] != outcome.request:
                raise ValueError("One operation identity has conflicting confirmed actions.")
        if outcome is not None and outcome.request.operation_id not in seen:
            seen[outcome.request.operation_id] = outcome.request
            action = ConfirmedAction(
                operation_id=outcome.request.operation_id,
                sequence=record.sequence,
                step=outcome.request.step,
                route=outcome.request.expected_route,
            )
            actions.append(action)
            if (
                before_valid
                and writes_known
                and not pending
                and before is not None
                and before_ref is not None
                and native.Location.model_validate(action.route.model_dump()) == before.location
            ):
                before_actions.append(
                    ActionObservation(
                        operation_id=action.operation_id,
                        before=EvidenceObservation(ref=before_ref, observation=before),
                    )
                )
            pending.append(action)
            if isinstance(action.step, native.UsePotion):
                entry = (
                    before.objects.get(action.step.potion)
                    if before is not None
                    and native.Location.model_validate(action.route.model_dump()) == before.location
                    else None
                )
                uses.append(
                    PotionUse(
                        operation_id=action.operation_id,
                        sequence=record.sequence,
                        definition_id=entry.definition_id
                        if isinstance(entry, native.PotionEntry)
                        else None,
                    )
                )
        if unknown_write or outcome is not None:
            before_valid = False
        if record.reply is None:
            before_valid = False
            uncertain.append(Uncertainty.INCOMPLETE_RECORDS)
            continue
        if not record.reply.observations:
            continue
        after = record.reply.observations[-1]
        after_ref = ObservationRef(
            recording_id=recording_id,
            sequence=record.sequence,
            index=len(record.reply.observations) - 1,
        )
        if (
            len(pending) == 1
            and writes_known
            and before is not None
            and before_ref is not None
            and before.location.game_instance_id == after.location.game_instance_id
            and before.location.run_id == after.location.run_id
        ):
            choice = _reward(before, before_ref, after, after_ref, pending[0])
            if choice is not None:
                reward = choice
        pending.clear()
        writes_known = True
        before, before_ref = after, after_ref
        before_valid = not unknown_write
    endpoint = None
    endpoint_ref = None
    if records:
        last = records[-1]
        if last.reply is not None and last.reply.observations:
            candidate = last.reply.observations[-1]
            if (
                target is not None
                and candidate.location.game_instance_id == target.location.game_instance_id
                and candidate.location.run_id == target.location.run_id
            ):
                endpoint = candidate
                endpoint_ref = ObservationRef(
                    recording_id=recording_id,
                    sequence=last.sequence,
                    index=len(last.reply.observations) - 1,
                )
    target_hp = target.state.player.hp if target is not None else None
    endpoint_hp = endpoint.state.player.hp if endpoint is not None else None
    result = (
        endpoint.state.result
        if endpoint and isinstance(endpoint.state, native.TerminalState)
        else None
    )
    if target is None:
        uncertain.append(Uncertainty.NO_TARGET)
    if endpoint is None:
        uncertain.append(Uncertainty.NO_ENDPOINT)
    if result is None:
        uncertain.append(Uncertainty.NO_NATIVE_RESULT)
    metrics = SampleMetrics(
        target_hp=target_hp,
        endpoint_hp=endpoint_hp,
        net_hp_change=endpoint_hp - target_hp
        if target_hp is not None and endpoint_hp is not None
        else None,
        native_result=result,
        potion_uses=tuple(uses) if records else None,
        reward=reward,
    )
    return _Evidence(
        SampleEvidence(actions=tuple(actions), metrics=metrics),
        EvidenceObservation(ref=endpoint_ref, observation=endpoint)
        if endpoint_ref is not None and endpoint is not None
        else None,
        tuple(before_actions),
        tuple(uncertain),
    )


async def assess_sample(
    case: Case,
    execution: TakeoverResult | ExecutionRecord,
    annotations: AnnotationStore,
    selection: AssessmentSelection,
    *,
    judgment: HumanJudgment | SemanticJudge | None,
) -> SampleAssessment:
    record = (
        execution
        if isinstance(execution, (CompletedExecution, IncompleteExecution, UnsupportedExecution))
        else execution_record(execution)
    )
    standards = annotations.select(case, selection)
    branch = record.branch.resolve()
    identity = (
        archive_identity(branch) if record.recording_id is not None and branch.exists() else None
    )
    if identity is not None and identity.recording_id != record.recording_id:
        raise ValueError("The sample recording does not match this execution.")
    if identity is None and isinstance(record, CompletedExecution):
        raise ValueError("A completed sample requires its closed recording archive.")
    parent = None
    variant = None
    records: tuple[Record, ...] = ()
    first_sequence = None
    last_sequence = None
    gaps = False
    target = None
    target_ref = None
    if identity is not None:
        with closing(Journal.open(branch)) as journal:
            binding = bind_sample(case, annotations.cases, journal, record)
            parent = binding.parent
            variant = binding.variant
            target = binding.target
            target_ref = binding.target_ref
            first_sequence = binding.first_sequence
            last_sequence = binding.last_sequence
            gaps = binding.gaps
            records = () if first_sequence is None else journal.operations(start=first_sequence)
            if not isinstance(variant, SceneVariant):
                anchor = None if isinstance(record, UnsupportedExecution) else record.anchor
                if anchor is not None and (
                    anchor.anchor.recording_id != case.source.anchor.recording_id
                    or anchor.anchor.model_sequence != case.source.anchor.model_sequence
                    or anchor.confirmed_operation_cursor != case.source.target_cursor
                    or anchor.semantic_digest != case.source.semantic_digest
                ):
                    raise ValueError("The execution anchor does not match the case.")
                if variant is not None:
                    original = annotations.cases.load(variant.case_id)
                    if (
                        sha256(original.model_dump_json().encode()).hexdigest()
                        != variant.case_sha256
                        or variant.source.archive.resolve() != original.source.archive.resolve()
                        or variant.source.recording_id != original.source.anchor.recording_id
                        or variant.source.before_sequence != original.source.anchor.model_sequence
                    ):
                        raise ValueError("The material variant does not match its original case.")
            if isinstance(record, CompletedExecution):
                endpoint = record.endpoint
                expected = case.endpoint
                if isinstance(expected, AfterDecisionsSpec):
                    if (
                        not isinstance(endpoint, DecisionEndpoint)
                        or endpoint.count != expected.count
                    ):
                        raise ValueError("The completed endpoint does not match the case.")
                elif (
                    not isinstance(endpoint, BattleEndpoint)
                    or endpoint.node_id != target.location.node_id
                ):
                    raise ValueError("The completed battle endpoint does not match the case.")
    projection = _evidence(
        records,
        target,
        target_ref,
        identity.recording_id if identity else "",
    )
    evidence = projection.sample
    endpoint_ref = projection.endpoint.ref if projection.endpoint is not None else None
    assert endpoint_ref is None or isinstance(endpoint_ref, ObservationRef)
    uncertainty = projection.uncertainty
    if gaps:
        uncertainty += (Uncertainty.INCOMPLETE_RECORDS,)
    if identity is None:
        evidence = SampleEvidence(
            actions=(),
            metrics=SampleMetrics(
                target_hp=None,
                endpoint_hp=None,
                net_hp_change=None,
                native_result=None,
                potion_uses=None,
                reward=None,
            ),
        )
        uncertainty = (Uncertainty.NO_ARCHIVE,)
    if isinstance(judgment, HumanJudgment) and (
        judgment.case_id != case.id
        or judgment.sample != identity
        or judgment.selection != selection
    ):
        raise ValueError("The human judgment must bind this case, sample and selected standards.")
    quality: Quality
    if standards.annotation is None and standards.rubric is None:
        quality = Unscored()
    elif (
        not isinstance(record, CompletedExecution) or target is None or endpoint_ref is None or gaps
    ):
        quality = Unassessed(reason=UnassessedReason.INSUFFICIENT_EVIDENCE)
    elif (
        standards.rubric is not None
        and isinstance(standards.rubric.criterion, CardRewardRubric)
        and evidence.metrics.reward is None
    ):
        quality = Unassessed(reason=UnassessedReason.INSUFFICIENT_EVIDENCE)
        uncertainty += (Uncertainty.REWARD_EVIDENCE,)
    elif judgment is None:
        quality = Unassessed(reason=UnassessedReason.NO_JUDGMENT)
    else:
        assert identity is not None
        result = (
            judgment
            if isinstance(judgment, HumanJudgment)
            else await judgment.assess(
                JudgmentInput(
                    case_id=case.id,
                    sample=identity,
                    standards=standards,
                    evidence=evidence,
                    endpoint_reached=True,
                    uncertainty=tuple(dict.fromkeys(uncertainty)),
                    target=EvidenceObservation(ref=target_ref, observation=target)
                    if target_ref is not None
                    else None,
                    endpoint=projection.endpoint,
                    before_actions=projection.before_actions,
                )
            )
        )
        if result.case_id != case.id or result.sample != identity or result.selection != selection:
            raise ValueError("The judge result must bind this case, sample and selected standards.")
        quality = (
            Unassessed(reason=UnassessedReason.JUDGE_FAILED, failure=result)
            if isinstance(result, JudgeFailed)
            else Assessed(judgment=result)
        )
    if identity is not None and archive_identity(branch) != identity:
        raise ValueError("The sample archive changed during assessment.")
    return SampleAssessment(
        execution=record,
        endpoint_reached=isinstance(record, CompletedExecution),
        evidence=evidence,
        quality=quality,
        provenance=SampleProvenance(
            case_id=case.id,
            case_sha256=sha256(case.model_dump_json().encode()).hexdigest(),
            source=case.source,
            archive=branch,
            sample=identity,
            parent=parent,
            variant=variant,
            first_sequence=first_sequence,
            last_sequence=last_sequence,
            target=target_ref,
            endpoint=endpoint_ref,
        ),
        standards=standards,
        uncertainty=tuple(dict.fromkeys(uncertainty)),
    )
