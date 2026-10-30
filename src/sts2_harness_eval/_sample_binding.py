"""Bind recorded samples to their case, target observation and evidence interval."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import file_digest, sha256

from sts2_harness.client import models as native
from sts2_harness.journal import (
    DependencyKind,
    HistoricalVariant,
    Journal,
    ModelSummary,
    ParentPrefix,
    SceneVariant,
)
from sts2_harness_eval.assessment_records import CaseTargetRef, SceneInitialRef
from sts2_harness_eval.cases import AfterDecisionsSpec, Case, CaseStore
from sts2_harness_eval.checkpoints import ReloadTarget
from sts2_harness_eval.execution_record import (
    AnchorRecord,
    BattleEndpoint,
    CompletedExecution,
    DecisionEndpoint,
    ExecutionRecord,
    ReachedRecord,
    UnsupportedExecution,
)


@dataclass(frozen=True, slots=True)
class SampleBinding:
    parent: ParentPrefix | None
    variant: HistoricalVariant | SceneVariant | None
    target: native.Observation
    target_ref: CaseTargetRef | SceneInitialRef
    first_sequence: int | None
    last_sequence: int | None
    gaps: bool


def bind_sample(
    case: Case, cases: CaseStore, journal: Journal, execution: ExecutionRecord
) -> SampleBinding:
    parent = journal.manifest.parent
    variant = journal.manifest.variant
    if isinstance(variant, SceneVariant):
        _validate_scene_sample(case, cases, journal, execution)
        target = variant.initial.observation
        target_ref = SceneInitialRef(recording_id=journal.manifest.recording_id)
        summaries = journal.summaries()
        first_sequence = min((item.sequence for item in summaries), default=None)
    else:
        if parent is None:
            raise ValueError("Historical assessment requires a direct parent.")
        if (
            parent.archive.resolve() != case.source.archive.resolve()
            or parent.recording_id != case.source.anchor.recording_id
            or parent.before_sequence != case.source.anchor.model_sequence
        ):
            raise ValueError("The sample parent does not match the case source anchor.")
        anchor = None if isinstance(execution, UnsupportedExecution) else execution.anchor
        if anchor is not None and (
            anchor.anchor.recording_id != case.source.anchor.recording_id
            or anchor.anchor.model_sequence != case.source.anchor.model_sequence
            or anchor.confirmed_operation_cursor != case.source.target_cursor
            or anchor.semantic_digest != case.source.semantic_digest
        ):
            raise ValueError("The execution anchor does not match the case.")
        if variant is not None:
            original = cases.load(variant.case_id)
            if (
                sha256(original.model_dump_json().encode()).hexdigest() != variant.case_sha256
                or variant.source.archive.resolve() != original.source.archive.resolve()
                or variant.source.recording_id != original.source.anchor.recording_id
                or variant.source.before_sequence != original.source.anchor.model_sequence
            ):
                raise ValueError("The material variant does not match its original case.")
        binding = cases.binding(case)
        reference = cases.reference(binding).target
        target = reference.read.observation
        target_ref = CaseTargetRef(
            case_id=case.id,
            reference_sha256=binding.reference,
            recording_id=case.source.anchor.recording_id,
            sequence=reference.source_sequence,
        )
        summaries = journal.summaries(start=parent.before_sequence)
        first_sequence = parent.before_sequence
    if isinstance(execution, CompletedExecution):
        endpoint = execution.endpoint
        expected = case.endpoint
        if isinstance(expected, AfterDecisionsSpec):
            if not isinstance(endpoint, DecisionEndpoint) or endpoint.count != expected.count:
                raise ValueError("The completed endpoint does not match the case.")
        elif (
            not isinstance(endpoint, BattleEndpoint) or endpoint.node_id != target.location.node_id
        ):
            raise ValueError("The completed battle endpoint does not match the case.")
    return SampleBinding(
        parent=parent,
        variant=variant,
        target=target,
        target_ref=target_ref,
        first_sequence=first_sequence,
        last_sequence=max((item.sequence for item in summaries), default=None),
        gaps=bool(journal.gaps),
    )


def _validate_scene_sample(
    case: Case, cases: CaseStore, journal: Journal, execution: ExecutionRecord
) -> None:
    variant = journal.manifest.variant
    assert isinstance(variant, SceneVariant)
    source = variant.source
    if (
        variant.case_id != case.id
        or variant.case_sha256 != sha256(case.model_dump_json().encode()).hexdigest()
        or source.archive.resolve() != case.source.archive.resolve()
        or source.recording_id != case.source.anchor.recording_id
        or source.model_sequence != case.source.anchor.model_sequence
    ):
        raise ValueError("The Scene declaration does not match this case and source anchor.")
    with source.archive.open("rb") as original:
        digest = file_digest(original, "sha256").hexdigest()
    with closing(Journal.open(source.archive)) as original:
        if digest != source.archive_sha256 or original.manifest.recording_id != source.recording_id:
            raise ValueError("The Scene source archive has changed.")
    initial = variant.initial
    restore = initial.restore
    recipe = ReloadTarget(
        canonical_seed=restore.canonical_seed,
        game_mode=restore.game_mode,
        character=restore.character,
        ascension=restore.ascension,
        act=restore.act,
        floor=restore.floor,
    )
    binding = cases.binding(case)
    reference = cases.reference(binding)
    location = initial.observation.location
    # The load route precedes suffix replay; it retains the boundary role, not the target node.
    if (
        restore.checkpoint_sha256 != case.checkpoint.sha256
        or restore.checkpoint_bytes != case.checkpoint.bytes
        or recipe != case.checkpoint.target
        or reference.boundary.source_sequence != case.checkpoint.boundary_sequence
        or reference.target.source_sequence != case.source.anchor.model_sequence
        or binding.target.source_sequence != case.source.anchor.model_sequence
        or restore.route.game_instance_id != location.game_instance_id
        or restore.route.run_id != location.run_id
        or restore.route.owner != reference.boundary.read.observation.location.owner
        or location.owner != reference.target.read.observation.location.owner
    ):
        raise ValueError("The Scene initial conditions do not match the case restore declarations.")
    first = next(
        (
            item
            for item in journal.summaries(kind=DependencyKind.MODEL)
            if isinstance(item, ModelSummary)
        ),
        None,
    )
    anchor = None if isinstance(execution, UnsupportedExecution) else execution.anchor
    if first is None:
        if anchor is not None or isinstance(execution, CompletedExecution):
            raise ValueError("The Scene execution anchor has no recorded model request.")
        return
    if (
        first.role is not native.DecisionOwner.COMBAT
        or first.stream != "combat:1"
        or first.position != 1
    ):
        raise ValueError("The Scene anchor is not its first fresh Combat request.")
    if isinstance(execution, UnsupportedExecution):
        return
    request = journal.request(first.sequence)
    expected = ReachedRecord(
        anchor=AnchorRecord(
            recording_id=journal.manifest.recording_id, model_sequence=first.sequence
        ),
        confirmed_operation_cursor=first.confirmed_operation_cursor,
        semantic_digest=request.semantic_digest,
    )
    if anchor != expected:
        raise ValueError("The Scene execution anchor does not match its first fresh model request.")
