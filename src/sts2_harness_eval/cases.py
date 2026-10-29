"""Admit immutable, individually verified gameplay cases without model calls."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Never
from uuid import uuid4

import httpx2 as httpx
from pydantic import Field, TypeAdapter, ValidationError

from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.game import (
    ControlWriteError,
    GameLifetimeEnded,
    GameReadError,
    Settle,
    Settlement,
)
from sts2_harness.headless import HeadlessError
from sts2_harness.journal import ArchiveError, Journal, Manifest, Record
from sts2_harness.native_template import TemplateError, digest
from sts2_harness_eval.checkpoints import (
    CheckpointRejected,
    CheckpointUncertain,
    ReloadTarget,
    Sha256,
)
from sts2_harness_eval.replay import reach
from sts2_harness_eval.results import ModelAnchor, Reached
from sts2_harness_eval.verification import (
    AllowedMapDifference,
    KeyedRefs,
    Reference,
    Relation,
    VerificationFailure,
)

if TYPE_CHECKING:
    from sts2_harness_eval.native import NativeLaunch

type CaseId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")]
type Cursor = Annotated[int, Field(ge=0)]
type Sequence = Annotated[int, Field(gt=0)]


class SourceRef(native.WireModel):
    archive: Path
    anchor: ModelAnchor
    semantic_digest: Sha256
    target_cursor: Cursor


class CheckpointRef(native.WireModel):
    sha256: Sha256
    bytes: Annotated[int, Field(gt=0)]
    boundary_sequence: Sequence
    boundary_cursor: Cursor
    target: ReloadTarget


class EndpointKind(StrEnum):
    AFTER_DECISIONS = "after_decisions"
    TARGET_BATTLE = "target_battle"


class AfterDecisionsSpec(native.WireModel):
    kind: Literal[EndpointKind.AFTER_DECISIONS] = EndpointKind.AFTER_DECISIONS
    count: Annotated[int, Field(gt=0)]


class TargetBattle(native.WireModel):
    kind: Literal[EndpointKind.TARGET_BATTLE] = EndpointKind.TARGET_BATTLE


type CaseEndpoint = Annotated[AfterDecisionsSpec | TargetBattle, Field(discriminator="kind")]


class Case(native.WireModel):
    schema_version: Literal[1] = 1
    id: CaseId
    source: SourceRef
    checkpoint: CheckpointRef
    binding: Sha256
    evidence: Sha256
    endpoint: CaseEndpoint


class ShortcutBinding(native.WireModel):
    suffix: tuple[KeyedRefs, ...]
    target: KeyedRefs
    follow_on: tuple[KeyedRefs, ...]
    reference: Sha256


class AdmissionRequest(native.WireModel):
    id: CaseId
    archive: Path
    anchor: ModelAnchor
    endpoint: CaseEndpoint


class UnsupportedCause(StrEnum):
    HISTORY = "history"
    INITIAL_CONDITIONS = "initial_conditions"
    TARGET = "target"
    NO_BOUNDARY = "no_boundary"
    BOUNDARY = "boundary"
    FOLLOW_ON = "follow_on"
    UNKNOWN_WRITE = "unknown_write"
    IDENTITY = "identity"
    NATIVE_REJECTED = "native_rejected"
    DEPENDENCY = "dependency"
    CASE_EXISTS = "case_exists"


class Stage(StrEnum):
    PREPARATION = "preparation"
    CAPTURE = "capture"
    LOAD = "load"
    PUBLICATION = "publication"


class IncompleteCause(StrEnum):
    NATIVE_UNCERTAIN = "native_uncertain"
    IO = "io"
    PROTOCOL = "protocol"
    STARTUP = "startup"


class Admitted(native.WireModel):
    case_id: CaseId


class Unsupported(native.WireModel):
    cause: UnsupportedCause
    sequence: int | None


class Mismatch(native.WireModel):
    relation: Relation
    sequence: int | None
    field_path: str


class Incomplete(native.WireModel):
    stage: Stage
    cause: IncompleteCause


type AdmissionResult = Admitted | Unsupported | Mismatch | Incomplete


class AdmissionEvidence(native.WireModel):
    request: AdmissionRequest
    stage: Stage
    result: AdmissionResult | None
    differences: tuple[AllowedMapDifference, ...] = ()


class AdmissionFailure(Exception):
    def __init__(self, result: Unsupported | Mismatch) -> None:
        self.result = result
        super().__init__(str(result))


@dataclass(frozen=True)
class ReplayWrite:
    sequence: int
    cursor: int
    before: native.Observation
    step: native.NativeStep
    after: native.Observation


@dataclass(frozen=True)
class AdmissionPlan:
    source: SourceRef
    manifest: Manifest
    boundary_sequence: int
    boundary_cursor: int
    boundary: native.Observation
    prefix: tuple[ReplayWrite, ...]
    suffix: tuple[ReplayWrite, ...]
    target: native.Observation
    follow_on: tuple[ReplayWrite, ...]
    staging: Path


def _unsupported(cause: UnsupportedCause, sequence: int | None = None) -> Never:
    raise AdmissionFailure(Unsupported(cause=cause, sequence=sequence))


def _confirmed_write(
    record: Record, before: native.Observation | None, publications: Iterable[Record]
) -> ReplayWrite | None:
    if not isinstance(record.request, Settle):
        return None
    reply = record.reply
    assert reply is not None
    outcome = reply.outcome
    if (
        not isinstance(outcome, Settlement)
        or outcome.receipt is None
        or isinstance(outcome.receipt, native.ReceiptUnknown)
    ):
        _unsupported(UnsupportedCause.UNKNOWN_WRITE, record.sequence)
    if not isinstance(outcome.receipt, native.ReceiptCompleted):
        return None
    if (
        outcome.request.expected_route != record.request.route
        or outcome.request.step != record.request.step
        or outcome.request.operation_id != record.operation_id
        or outcome.receipt.operation_id != record.operation_id
    ):
        _unsupported(UnsupportedCause.HISTORY, record.sequence)
    for publication in publications:
        if publication.sequence != record.sequence and isinstance(publication.request, Settle):
            break
        if publication.reply is None or not publication.reply.observations:
            continue
        if before is None:
            _unsupported(UnsupportedCause.HISTORY, record.sequence)
        return ReplayWrite(
            record.sequence,
            record.confirmed_operation_cursor,
            before,
            record.request.step,
            publication.reply.observations[-1],
        )
    _unsupported(UnsupportedCause.HISTORY, record.sequence)


def plan_admission(request: AdmissionRequest, staging: Path) -> AdmissionPlan:
    """Select one nearest map and the original confirmed follow-on interval."""
    with closing(Journal.open(request.archive)) as journal:
        manifest = journal.manifest
        records = journal.operations()
        target = next(
            (
                item
                for item in journal.requests(stop=request.anchor.model_sequence)
                if item.sequence == request.anchor.model_sequence
            ),
            None,
        )
        if target is None or manifest.recording_id != request.anchor.recording_id:
            _unsupported(UnsupportedCause.TARGET)
        assert target is not None
        headless = manifest.headless
        if (
            headless is None
            or manifest.template is None
            or manifest.seed is None
            or manifest.seed != headless.start_receipt.seed
        ):
            _unsupported(UnsupportedCause.INITIAL_CONDITIONS)
        latest: native.Observation | None = None
        boundary: tuple[int, int, native.Observation] | None = None
        target_observation: native.Observation | None = None
        target_selected = False
        writes: list[ReplayWrite] = []
        follow: list[ReplayWrite] = []
        end_sequence: int | None = None
        cursor = 0
        for index, record in enumerate(records):
            if record.sequence >= target.sequence and not target_selected:
                target_observation = latest
                target_selected = True
                if target_observation is None:
                    _unsupported(UnsupportedCause.TARGET, target.sequence)
            reply = record.reply
            if reply is None:
                _unsupported(UnsupportedCause.HISTORY, record.sequence)
            assert reply is not None
            write = _confirmed_write(record, latest, islice(records, index, None))
            if write is not None:
                if write.cursor != cursor + 1:
                    _unsupported(UnsupportedCause.HISTORY, record.sequence)
                cursor = write.cursor
                if record.sequence < target.sequence:
                    writes.append(write)
                else:
                    assert target_observation is not None
                    same_node = (
                        write.before.location.node_id == target_observation.location.node_id
                        and write.after.location.node_id == target_observation.location.node_id
                    )
                    if isinstance(target_observation.state, native.RunState):
                        if not same_node or not _reward_follow_on(target_observation, write):
                            _unsupported(UnsupportedCause.FOLLOW_ON, record.sequence)
                        follow.append(write)
                        end_sequence = record.sequence
                    elif (
                        not isinstance(write.before.state, native.CombatState)
                        or not isinstance(write.after.state, native.CombatState)
                        or not same_node
                    ):
                        _unsupported(UnsupportedCause.FOLLOW_ON, record.sequence)
                    else:
                        follow.append(write)
                        if isinstance(write.step, native.EndTurn):
                            end_sequence = record.sequence
            if record.confirmed_operation_cursor != cursor:
                _unsupported(UnsupportedCause.HISTORY, record.sequence)
            if reply.observations:
                latest = reply.observations[-1]
                state = latest.state
                if (
                    record.sequence < target.sequence
                    and isinstance(state, native.RunState)
                    and state.screen is native.RunScreen.MAP
                    and latest.input_readiness is native.InputReadiness.READY
                    and state.selection is None
                    and state.actions
                ):
                    boundary = (record.sequence, record.confirmed_operation_cursor, latest)
            if end_sequence is not None:
                break
        if target_observation is None:
            target_observation = latest
        if target_observation is None or not (
            isinstance(target_observation.state, native.CombatState)
            or (
                isinstance(request.endpoint, AfterDecisionsSpec)
                and request.endpoint.count == 1
                and _card_reward_selection(target_observation)
            )
        ):
            _unsupported(UnsupportedCause.TARGET, target.sequence)
        assert target_observation is not None
        if end_sequence is None:
            _unsupported(UnsupportedCause.FOLLOW_ON, target.sequence)
        if any(gap.first_sequence <= end_sequence for gap in journal.gaps):
            _unsupported(UnsupportedCause.HISTORY)
        if boundary is None:
            _unsupported(UnsupportedCause.NO_BOUNDARY)
        assert boundary is not None
        sequence, cursor, observation = boundary
        if observation.state.progress.room_type not in ("monster", "elite", "boss", "event"):
            _unsupported(UnsupportedCause.BOUNDARY, sequence)
        if target.confirmed_operation_cursor < cursor:
            _unsupported(UnsupportedCause.TARGET, target.sequence)
        return AdmissionPlan(
            SourceRef(
                archive=request.archive.resolve(),
                anchor=request.anchor,
                semantic_digest=target.semantic_digest,
                target_cursor=target.confirmed_operation_cursor,
            ),
            manifest,
            sequence,
            cursor,
            observation,
            tuple(write for write in writes if write.cursor <= cursor),
            tuple(
                write
                for write in writes
                if cursor < write.cursor <= target.confirmed_operation_cursor
            ),
            target_observation,
            tuple(follow),
            staging,
        )


def _card_reward_selection(observation: native.Observation) -> bool:
    state = observation.state
    if not isinstance(state, native.RunState) or state.screen is not native.RunScreen.REWARDS:
        return False
    selection = state.selection
    if (
        observation.input_readiness is not native.InputReadiness.SELECTION
        or selection is None
        or selection.min != 0
        or selection.max != 1
        or selection.selected
        or not selection.candidates
        or not all(
            isinstance(observation.objects.get(ref), native.CardEntry)
            for ref in selection.candidates
        )
    ):
        return False
    return any(
        offer.kind is native.OfferKind.CARD and set(offer.contents) == set(selection.candidates)
        for offer in state.offers
    )


def _reward_follow_on(target: native.Observation, write: ReplayWrite) -> bool:
    if not _card_reward_selection(target) or not _card_reward_selection(write.before):
        return False
    state = write.before.state
    assert isinstance(state, native.RunState) and state.selection is not None
    step = write.step
    if isinstance(step, native.Select):
        valid_choice = len(step.refs) <= 1 and set(step.refs) <= set(state.selection.candidates)
    elif isinstance(step, native.Interact):
        valid_choice = any(
            action.ref == step.action
            and action.enabled
            and action.subject in state.selection.candidates
            for action in state.actions
        )
    else:
        return False
    after = write.after
    return (
        valid_choice
        and after.input_readiness is native.InputReadiness.READY
        and isinstance(after.state, native.RunState)
        and after.state.screen is native.RunScreen.REWARDS
        and after.state.selection is None
    )


class Artifact(StrEnum):
    REFERENCES = "references"
    BINDINGS = "bindings"
    EVIDENCE = "evidence"


class CaseStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()

    def _path(self, group: Artifact, digest: str) -> Path:
        return self.directory / group / (digest + ".json")

    def _immutable(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / ("." + uuid4().hex)
        try:
            with temporary.open("xb") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise ArchiveError(
                        "An immutable case dependency already has different content."
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)

    def content(self, group: Artifact, value: native.WireModel) -> str:
        content = value.model_dump_json().encode()
        key = sha256(content).hexdigest()
        self._immutable(self._path(group, key), content)
        return key

    def _read(self, group: Artifact, key: str) -> bytes:
        content = self._path(group, key).read_bytes()
        if sha256(content).hexdigest() != key:
            raise ArchiveError("A case dependency has changed.")
        return content

    def checkpoint_path(self, checkpoint: CheckpointRef) -> Path:
        path = self.directory / "checkpoints" / checkpoint.sha256 / "current_run.save"
        if path.stat().st_size != checkpoint.bytes or digest(path) != checkpoint.sha256:
            raise ArchiveError("A case checkpoint has changed.")
        return path

    def retain_checkpoint(self, checkpoint: CheckpointRef, path: Path) -> None:
        if path.stat().st_size != checkpoint.bytes or digest(path) != checkpoint.sha256:
            raise ArchiveError("The captured checkpoint does not match its native proof.")
        self._immutable(
            self.directory / "checkpoints" / checkpoint.sha256 / "current_run.save",
            path.read_bytes(),
        )

    def binding(self, case: Case) -> ShortcutBinding:
        return ShortcutBinding.model_validate_json(self._read(Artifact.BINDINGS, case.binding))

    def reference(self, binding: ShortcutBinding) -> Reference:
        return Reference.model_validate_json(self._read(Artifact.REFERENCES, binding.reference))

    def load(self, case_id: str) -> Case:
        case_id = TypeAdapter(CaseId).validate_python(case_id)
        case = Case.model_validate_json((self.directory / "cases" / f"{case_id}.json").read_bytes())
        if case.id != case_id:
            raise ArchiveError("The requested case identity has changed.")
        self.checkpoint_path(case.checkpoint)
        self.reference(self.binding(case))
        self._read(Artifact.EVIDENCE, case.evidence)
        with closing(Journal.open(case.source.archive)) as journal:
            if journal.manifest.recording_id != case.source.anchor.recording_id:
                raise ArchiveError("The referenced source recording has changed.")
            target = next(
                (
                    record
                    for record in journal.requests()
                    if record.sequence == case.source.anchor.model_sequence
                ),
                None,
            )
            if (
                target is None
                or target.semantic_digest != case.source.semantic_digest
                or target.confirmed_operation_cursor != case.source.target_cursor
            ):
                raise ArchiveError("The referenced source request has changed.")
        return case

    def existing(self, source: SourceRef, sequence: int) -> CheckpointRef | None:
        for path in sorted((self.directory / "cases").glob("*.json")):
            case = Case.model_validate_json(path.read_bytes())
            if (
                case.source.anchor.recording_id == source.anchor.recording_id
                and case.checkpoint.boundary_sequence == sequence
            ):
                return self.load(path.stem).checkpoint
        return None

    def publish(self, case: Case) -> None:
        self.checkpoint_path(case.checkpoint)
        self.reference(self.binding(case))
        self._read(Artifact.EVIDENCE, case.evidence)
        self._immutable(
            self.directory / "cases" / f"{case.id}.json", case.model_dump_json().encode()
        )


async def admit_case(
    request: AdmissionRequest, launch: NativeLaunch, store: CaseStore
) -> AdmissionResult:
    from sts2_harness_eval.native import NativeRestorer

    stage = Stage.PREPARATION
    result: AdmissionResult
    try:
        if (store.directory / "cases" / f"{request.id}.json").exists():
            return Unsupported(cause=UnsupportedCause.CASE_EXISTS, sequence=None)
        store.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".admission-", dir=store.directory) as temporary:
            plan = plan_admission(request, Path(temporary))
            async with reach(request.archive, request.anchor) as reached:
                if not isinstance(reached, Reached):
                    _unsupported(UnsupportedCause.HISTORY)
                assert isinstance(reached, Reached)
                if (
                    reached.confirmed_operation_cursor != plan.source.target_cursor
                    or reached.semantic_digest != plan.source.semantic_digest
                ):
                    _unsupported(UnsupportedCause.HISTORY)
            restorer = NativeRestorer(launch, store)
            stage = Stage.CAPTURE
            captured = await restorer.capture(
                plan, store.existing(plan.source, plan.boundary_sequence)
            )
            store.retain_checkpoint(captured.checkpoint, captured.save)
            reference_key = store.content(Artifact.REFERENCES, captured.reference)
            binding = ShortcutBinding(
                suffix=captured.suffix,
                target=captured.target,
                follow_on=captured.follow_on,
                reference=reference_key,
            )
            binding_key = store.content(Artifact.BINDINGS, binding)
            captured_evidence = store.content(
                Artifact.EVIDENCE, AdmissionEvidence(request=request, stage=Stage.LOAD, result=None)
            )
            provisional = Case(
                id=request.id,
                source=plan.source,
                checkpoint=captured.checkpoint,
                binding=binding_key,
                evidence=captured_evidence,
                endpoint=request.endpoint,
            )
            stage = Stage.LOAD
            async with restorer.loaded(provisional) as loaded:
                await restorer.verify_follow_on(loaded, plan, captured.reference, binding)
                differences = loaded.differences
        stage = Stage.PUBLICATION
        result = Admitted(case_id=request.id)
        evidence_key = store.content(
            Artifact.EVIDENCE,
            AdmissionEvidence(request=request, stage=stage, result=result, differences=differences),
        )
        store.publish(provisional.model_copy(update={"evidence": evidence_key}))
        return result
    except AdmissionFailure as error:
        result = error.result
    except VerificationFailure as error:
        result = (
            Unsupported(cause=UnsupportedCause.IDENTITY, sequence=error.sequence)
            if error.unsupported
            else Mismatch(
                relation=error.relation, sequence=error.sequence, field_path=error.field_path
            )
        )
    except CheckpointRejected:
        result = Unsupported(cause=UnsupportedCause.NATIVE_REJECTED, sequence=None)
    except CheckpointUncertain:
        result = Incomplete(stage=stage, cause=IncompleteCause.NATIVE_UNCERTAIN)
    except (OSError, ArchiveError, httpx.HTTPError, TimeoutError):
        result = Incomplete(stage=stage, cause=IncompleteCause.IO)
    except ValidationError:
        result = Incomplete(stage=stage, cause=IncompleteCause.PROTOCOL)
    except (HeadlessError, TemplateError):
        result = Incomplete(stage=stage, cause=IncompleteCause.STARTUP)
    except (ClientUnavailable, GameReadError, ControlWriteError, GameLifetimeEnded):
        result = Incomplete(stage=stage, cause=IncompleteCause.NATIVE_UNCERTAIN)
    try:
        store.content(
            Artifact.EVIDENCE, AdmissionEvidence(request=request, stage=stage, result=result)
        )
    except (OSError, ArchiveError):
        return Incomplete(stage=stage, cause=IncompleteCause.IO)
    return result
