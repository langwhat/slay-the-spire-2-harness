"""Derive encounter identities from a verified historical native archive."""

import argparse
import json
import sqlite3
import sys
import zlib
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import MISSING, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from sts2_harness.client import models as native
from sts2_harness.fights import Fight, FightHistory
from sts2_harness.game import OperationKind
from sts2_harness_eval.decision_cases import DecisionCase, TaskEvent
from sts2_harness_eval.stable_suite import InferredValue, infer_old_run_player


class ReplayStatus(StrEnum):
    VERIFIED = "verified"
    SYNTHETIC = "synthetic"


class FightResolution(StrEnum):
    RECOVERED = "recovered"
    PRESENT = "present"
    UNKNOWN = "unknown"


class ReplayAudit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    validated_model_sequences: tuple[int, ...]


class ReplayEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: ReplayStatus
    case_semantically_equal: bool
    anchor: int
    anchor_cursor: int
    archive_sha256_before: str
    archive_sha256_after: str
    archive_wal_clear_before: bool
    archive_wal_clear_after: bool
    source_case_sha256: str
    anchor_semantic_digest: str
    anchor_protocol_digests: tuple[str, ...]
    audit: ReplayAudit


class HistoricalEnrichmentError(ValueError):
    """The input cannot be bound to a verified native archive prefix."""


class _ArchivedInput(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    semantic_digest: str | None = None


class _ArchivedAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    protocol_encoding_digest: str | None = None


class _ArchivedReply(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    observations: tuple[str, ...]


class _RecordedState(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    fights: list[JsonValue] | None = None


# Case files from other source versions are compared as recorded JSON, not as current models.
class _RecordedTask(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    kind: Literal["task"]
    observation: JsonValue = None
    state: _RecordedState | None = None


class _RecordedEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _RecordedCase(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    source: JsonValue = None
    events: list[Annotated[_RecordedTask | _RecordedEvent, Field(union_mode="left_to_right")]]


@dataclass(frozen=True, slots=True)
class DerivedFiles:
    case_path: Path
    provenance_path: Path


@dataclass(frozen=True, slots=True)
class ObservationRef:
    sequence: int
    cursor: int
    sha256: str


@dataclass(slots=True)
class FightSegment:
    floor: int | None
    node: str | None
    history: FightHistory = field(default_factory=FightHistory)
    observations: list[ObservationRef] = field(default_factory=list[ObservationRef])
    names: set[str] = field(default_factory=set[str])
    identities: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    result: Fight | None = None


_OBJECT = TypeAdapter(dict[str, JsonValue])


def _sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def _decode[T: BaseModel](model: type[T], data: bytes, reason: str) -> T:
    try:
        return model.model_validate_json(data)
    except ValidationError:
        raise HistoricalEnrichmentError(reason) from None


def _blob(connection: sqlite3.Connection, digest: str) -> bytes:
    row = connection.execute("SELECT content FROM blobs WHERE digest = ?", (digest,)).fetchone()
    if row is None:
        raise HistoricalEnrichmentError("A recorded native blob is missing.")
    data = zlib.decompress(row[0])
    if _sha(data) != digest:
        raise HistoricalEnrichmentError("A recorded native blob has an invalid digest.")
    return data


def _old_observation(
    value: JsonValue, inferred: list[InferredValue], path: str
) -> native.Observation:
    infer_old_run_player(value, path, inferred)
    return native.Observation.model_validate_json(json.dumps(value))


def _last_source_task(source: _RecordedCase) -> _RecordedTask:
    for event in reversed(source.events):
        if isinstance(event, _RecordedTask):
            return event
    raise HistoricalEnrichmentError("The replayed source case has no task.")


def _archive_segments(
    connection: sqlite3.Connection,
    case: DecisionCase,
    proof: ReplayEvidence,
) -> tuple[list[FightSegment], list[InferredValue]]:
    anchor = case.source.model_sequence
    index = connection.execute(
        "SELECT sequence FROM records WHERE sequence <= ? UNION ALL "
        "SELECT sequence FROM model_requests WHERE sequence <= ? UNION ALL "
        "SELECT sequence FROM materials WHERE sequence <= ? ORDER BY sequence",
        (anchor, anchor, anchor),
    ).fetchall()
    if [row[0] for row in index] != list(range(1, anchor + 1)):
        raise HistoricalEnrichmentError("The archive prefix has a sequence gap or duplicate.")
    if connection.execute(
        "SELECT 1 FROM gaps WHERE first_sequence <= ? LIMIT 1", (anchor,)
    ).fetchone():
        raise HistoricalEnrichmentError("The archive records an incomplete prefix.")
    row = connection.execute(
        "SELECT cursor, input FROM model_requests WHERE sequence = ?", (anchor,)
    ).fetchone()
    if row is None or row[0] != proof.anchor_cursor:
        raise HistoricalEnrichmentError("The replayed model anchor does not match the archive.")
    reason = "The replayed semantic digest does not match the archive."
    model_input = _decode(_ArchivedInput, _blob(connection, row[1]), reason)
    if model_input.semantic_digest != proof.anchor_semantic_digest:
        raise HistoricalEnrichmentError(reason)
    attempts = connection.execute(
        "SELECT value FROM model_attempts WHERE sequence = ? ORDER BY attempt", (anchor,)
    ).fetchall()
    reason = "The replayed request digests do not match the archive."
    protocols = tuple(
        _decode(_ArchivedAttempt, _blob(connection, item[0]), reason).protocol_encoding_digest
        for item in attempts
    )
    if not protocols or protocols != proof.anchor_protocol_digests:
        raise HistoricalEnrichmentError(reason)

    event = case.events[0]
    assert isinstance(event, TaskEvent)
    expected = event.observation
    identity = (expected.location.game_instance_id, expected.location.run_id)
    if identity[1] is None:
        raise HistoricalEnrichmentError("The case has no run identity.")
    segments: list[FightSegment] = []
    active: FightSegment | None = None
    inferred: list[InferredValue] = []
    latest: native.Observation | None = None
    pending_change = False
    for sequence, cursor, kind, reply in connection.execute(
        "SELECT sequence, cursor, kind, reply FROM records WHERE sequence < ? ORDER BY sequence",
        (anchor,),
    ):
        if reply is None or cursor > proof.anchor_cursor:
            raise HistoricalEnrichmentError("A native operation in the prefix is incomplete.")
        recorded = _decode(_ArchivedReply, reply, "A native reply has no observation digest list.")
        if OperationKind(kind) in (OperationKind.SETTLE, OperationKind.PRESENT):
            pending_change = True
        for digest in recorded.observations:
            raw = _OBJECT.validate_json(_blob(connection, digest))
            observation = _old_observation(
                raw, inferred, f"records[{sequence}].observations[{digest}]"
            )
            if (observation.location.game_instance_id, observation.location.run_id) != identity:
                raise HistoricalEnrichmentError("The native prefix crosses a game or run boundary.")
            latest = observation
            pending_change = False
            state = observation.state
            if isinstance(state, native.CombatState):
                floor, node = state.progress.floor, observation.location.node_id
                if active is not None and (active.floor, active.node) != (floor, node):
                    segments.append(active)
                    active = None
                if active is None:
                    active = FightSegment(floor, node)
                encounter = observation.encounter
                if encounter is not MISSING:
                    active.names.add(encounter.name)
                    if encounter.definition_id is not MISSING:
                        active.identities.add((encounter.definition_id, encounter.name))
            if active is None:
                continue
            active.observations.append(ObservationRef(sequence, cursor, digest))
            active.history.observe(observation)
            if active.history.recent:
                active.result = active.history.recent[-1]
                segments.append(active)
                active = None
            elif isinstance(state, native.TerminalState) or (
                isinstance(state, native.RunState) and state.progress.floor != active.floor
            ):
                segments.append(active)
                active = None
    if active is not None:
        segments.append(active)
    if latest != expected or pending_change:
        raise HistoricalEnrichmentError("The source task is not the latest native observation.")
    return segments, inferred


def enrich_historical_case(
    case_path: Path,
    source_case_path: Path,
    replay_path: Path,
    expected_case_sha256: str,
    expected_replay_sha256: str,
    output: Path,
) -> DerivedFiles:
    """Write a new single-task case after checking source replay and native evidence."""
    case_bytes = case_path.read_bytes()
    replay_bytes = replay_path.read_bytes()
    source_bytes = source_case_path.read_bytes()
    if _sha(case_bytes) != expected_case_sha256 or _sha(replay_bytes) != expected_replay_sha256:
        raise HistoricalEnrichmentError("A case or replay proof has an unexpected digest.")
    proof = ReplayEvidence.model_validate_json(replay_bytes)
    if proof.status is not ReplayStatus.VERIFIED:
        raise HistoricalEnrichmentError("Synthetic replay evidence cannot be used for enrichment.")
    if (
        not proof.case_semantically_equal
        or proof.source_case_sha256 != _sha(source_bytes)
        or proof.anchor not in proof.audit.validated_model_sequences
        or proof.archive_sha256_before != proof.archive_sha256_after
        or not proof.archive_wal_clear_before
        or not proof.archive_wal_clear_after
    ):
        raise HistoricalEnrichmentError("The source replay does not verify the source case.")
    single_task = "Only a single-task frozen case can be enriched."
    frozen = _decode(_RecordedCase, case_bytes, single_task)
    if len(frozen.events) != 1 or not isinstance(task := frozen.events[0], _RecordedTask):
        raise HistoricalEnrichmentError(single_task)
    source = _decode(_RecordedCase, source_bytes, "The replayed source case has no events.")
    if frozen.source != source.source or task.observation != _last_source_task(source).observation:
        raise HistoricalEnrichmentError("The frozen case is not the replayed source task.")
    parsed = _OBJECT.validate_json(case_bytes)
    case_inferences: list[InferredValue] = []
    infer_old_run_player(parsed, "$", case_inferences)
    case = DecisionCase.model_validate_json(json.dumps(parsed))
    if proof.anchor != case.source.model_sequence or not isinstance(case.events[0], TaskEvent):
        raise HistoricalEnrichmentError("The replay proof names a different model anchor.")
    archive = case.source.archive
    if _sha(archive.read_bytes()) != proof.archive_sha256_before:
        raise HistoricalEnrichmentError("The archive digest differs from the replay evidence.")
    wal = Path(str(archive) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise HistoricalEnrichmentError(
            "An active archive WAL cannot be verified by its file digest."
        )
    with closing(sqlite3.connect(f"{archive.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        segments, inferred = _archive_segments(connection, case, proof)
    if wal.exists() and wal.stat().st_size:
        raise HistoricalEnrichmentError(
            "An active archive WAL cannot be verified by its file digest."
        )
    if _sha(archive.read_bytes()) != proof.archive_sha256_before:
        raise HistoricalEnrichmentError("The archive changed during native reconstruction.")

    if task.state is None or task.state.fights is None:
        raise HistoricalEnrichmentError("The frozen task has no recorded fight list.")
    fights = case.events[0].state.fights
    # The derived case is the frozen JSON as written, with encounter fields filled in place.
    original = _OBJECT.validate_json(case_bytes)
    events = original["events"]
    assert isinstance(events, list) and isinstance(events[0], dict)
    state = events[0]["state"]
    assert isinstance(state, dict)
    raw_fights = state["fights"]
    assert isinstance(raw_fights, list)
    positions = [
        position
        for fight in fights
        for position, segment in enumerate(segments)
        if segment.result is not None
        and segment.result.model_copy(update={"encounter": None})
        == fight.model_copy(update={"encounter": None})
    ]
    ordered = positions == sorted(set(positions))
    details: list[dict[str, JsonValue]] = []
    for index, fight in enumerate(fights):
        raw_fight = raw_fights[index]
        assert isinstance(raw_fight, dict)
        if "encounter" not in raw_fight:
            raw_fight["encounter"] = None
        candidates = [segment for segment in segments if segment.floor == fight.floor]
        resolution = FightResolution.UNKNOWN
        reason = "The native fight is absent or ambiguous."
        if fight.encounter is not None:
            resolution, reason = FightResolution.PRESENT, "The case already has an encounter."
        elif not ordered:
            reason = "The recorded fight order conflicts with the native history."
        elif (
            fight.floor is not None
            and sum(item.floor == fight.floor for item in fights) == 1
            and len(candidates) == 1
        ):
            segment = candidates[0]
            result = segment.result
            if len(segment.names) > 1 or len(segment.identities) > 1:
                reason = "The native encounter identity or name conflicts."
            elif (
                segment.node is not None
                and result is not None
                and result.encounter is not None
                and result.model_copy(update={"encounter": None}) == fight
            ):
                raw_fight["encounter"] = result.encounter.model_dump(mode="json")
                resolution, reason = FightResolution.RECOVERED, "The native fight uniquely matches."
        details.append(
            {
                "index": index,
                "floor": fight.floor,
                "resolution": resolution,
                "reason": reason,
                "segments": [
                    {
                        "node": segment.node,
                        "observations": [
                            {
                                "sequence": item.sequence,
                                "cursor": item.cursor,
                                "sha256": item.sha256,
                            }
                            for item in segment.observations
                        ],
                    }
                    for segment in candidates
                ],
            }
        )
    output.mkdir(parents=True, exist_ok=False)
    case_result = output / "case.json"
    provenance = output / "provenance.json"
    case_result.write_text(
        json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    provenance.write_text(
        json.dumps(
            {
                "source_replay": proof.status,
                "case_path": str(case_path),
                "case_sha256": expected_case_sha256,
                "source_case_path": str(source_case_path),
                "source_case_sha256": proof.source_case_sha256,
                "replay_path": str(replay_path),
                "replay_sha256": expected_replay_sha256,
                "archive_path": str(archive),
                "archive_sha256": proof.archive_sha256_before,
                "enrichment_code_sha256": _sha(Path(__file__).read_bytes()),
                "anchor": proof.anchor,
                "anchor_cursor": proof.anchor_cursor,
                "anchor_semantic_digest": proof.anchor_semantic_digest,
                "anchor_protocol_digests": list(proof.anchor_protocol_digests),
                "derived_case_sha256": _sha(case_result.read_bytes()),
                "inferred_values": [item.model_dump(mode="json") for item in case_inferences],
                "native_inferences": [item.model_dump(mode="json") for item in inferred],
                "fights": details,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return DerivedFiles(case_result, provenance)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--source-case", type=Path, required=True)
    parser.add_argument("--source-replay", type=Path, required=True)
    parser.add_argument("--case-sha256", required=True)
    parser.add_argument("--source-replay-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        enrich_historical_case(
            args.case,
            args.source_case,
            args.source_replay,
            args.case_sha256,
            args.source_replay_sha256,
            args.output,
        )
    except (OSError, ValueError, sqlite3.Error, zlib.error) as error:
        detail = str(error).partition("\n")[0]
        print(f"Historical enrichment error: {type(error).__name__}: {detail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
