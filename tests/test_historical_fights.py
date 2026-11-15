import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

import pytest
from pydantic import MISSING
from test_lifecycle import Campaign

from sts2_harness.client import models as native
from sts2_harness.fights import Fight, FightKind
from sts2_harness_eval.decision_cases import (
    DecisionCase,
    DecisionLabels,
    DecisionSource,
    HarnessState,
    TaskEvent,
)
from sts2_harness_eval.historical_fights import HistoricalEnrichmentError, enrich_historical_case


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture(tmp_path: Path, scenario: str = "known") -> tuple[Path, Path, Path, str, str]:
    archive = tmp_path / "source.sqlite"
    opening = (
        Campaign()
        .states[1]
        .model_copy(
            update={
                "encounter": native.NamedDefinition(name="Old enemy", definition_id="ENCOUNTER.OLD")
            }
        )
    )
    after = Campaign().states[2]
    assert isinstance(after.state, native.RunState)
    after = after.model_copy(
        update={
            "state": after.state.model_copy(
                update={
                    "progress": after.state.progress.model_copy(update={"floor": 1}),
                    "battle_turns": 4,
                }
            )
        }
    )
    observations = [opening, after]
    fights = (Fight(floor=1, kind=FightKind.NORMAL, turns=4, hp_in=10, hp_out=10),)
    if scenario == "missing":
        observations[0] = opening.model_copy(update={"encounter": MISSING})
    elif scenario == "different_id":
        observations.insert(
            1,
            opening.model_copy(
                update={
                    "encounter": native.NamedDefinition(
                        name="Old enemy", definition_id="ENCOUNTER.OTHER"
                    )
                }
            ),
        )
    elif scenario == "different_name_without_id":
        observations.insert(
            1, opening.model_copy(update={"encounter": native.NamedDefinition(name="Other enemy")})
        )
    elif scenario == "repeated":
        observations.extend((opening, after))
    elif scenario == "interrupted":
        busy = after.model_copy(
            update={
                "input_readiness": native.InputReadiness.BUSY,
                "state": after.state.model_copy(update={"control": native.StateControl.BUSY}),
            }
        )
        observations[1:1] = [busy, opening]
    elif scenario == "other_run":
        observations[0] = opening.model_copy(
            update={"location": opening.location.model_copy(update={"run_id": "other"})}
        )
    elif scenario == "reversed":
        assert isinstance(opening.state, native.CombatState)
        second = opening.model_copy(
            update={
                "location": opening.location.model_copy(update={"node_id": "battle2"}),
                "state": opening.state.model_copy(
                    update={"progress": opening.state.progress.model_copy(update={"floor": 2})}
                ),
                "encounter": native.NamedDefinition(name="Other", definition_id="ENCOUNTER.OTHER"),
            }
        )
        after = after.model_copy(
            update={
                "state": after.state.model_copy(
                    update={"progress": after.state.progress.model_copy(update={"floor": 2})}
                )
            }
        )
        observations.extend((second, after))
        fights = (Fight(floor=2, kind=FightKind.NORMAL, turns=4, hp_in=10, hp_out=10), fights[0])
    db = sqlite3.connect(archive)
    db.executescript("""
        CREATE TABLE records(sequence INTEGER PRIMARY KEY, cursor INTEGER, kind TEXT, reply TEXT);
        CREATE TABLE model_requests(sequence INTEGER PRIMARY KEY, cursor INTEGER, input TEXT);
        CREATE TABLE model_attempts(sequence INTEGER, attempt INTEGER, value TEXT);
        CREATE TABLE materials(sequence INTEGER PRIMARY KEY, cursor INTEGER);
        CREATE TABLE blobs(digest TEXT PRIMARY KEY, content BLOB);
        CREATE TABLE gaps(first_sequence INTEGER, last_sequence INTEGER);
    """)

    def blob(value: str) -> str:
        raw = value.encode()
        key = digest(raw)
        db.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (key, zlib.compress(raw)))
        return key

    for sequence, observation in enumerate(observations, 1):
        if scenario == "gap" and sequence == 2:
            continue
        key = blob(observation.model_dump_json())
        db.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?)",
            (sequence, sequence, "observe", json.dumps({"observations": [key]})),
        )
    input_hash = blob(json.dumps({"semantic_digest": "semantic"}))
    attempt_hash = blob(json.dumps({"protocol_encoding_digest": "protocol"}))
    anchor = len(observations) + 1
    if scenario == "unobserved_write":
        db.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?)",
            (anchor, anchor, "settle", json.dumps({"observations": []})),
        )
        anchor += 1
    db.execute("INSERT INTO model_requests VALUES (?, ?, ?)", (anchor, anchor - 1, input_hash))
    db.execute("INSERT INTO model_attempts VALUES (?, 1, ?)", (anchor, attempt_hash))
    if scenario == "post_anchor":
        later = opening.model_copy(
            update={
                "encounter": native.NamedDefinition(name="Other", definition_id="ENCOUNTER.OTHER")
            }
        )
        db.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?)",
            (
                anchor + 1,
                anchor,
                "observe",
                json.dumps({"observations": [blob(later.model_dump_json())]}),
            ),
        )
    db.commit()
    db.close()
    case = DecisionCase(
        source=DecisionSource(
            round="test", seed="test", floor=1, model_sequence=anchor, archive=archive
        ),
        labels=DecisionLabels(comment="test"),
        recorded_choices=(),
        recorded_reason="",
        events=(
            TaskEvent(
                observation=after,
                state=HarnessState(fights=fights),
            ),
        ),
        decision_tool="run_deck_choice",
    )
    case_path = tmp_path / "case.json"
    old_case = json.loads(case.model_dump_json())
    for fight in old_case["events"][0]["state"]["fights"]:
        del fight["encounter"]
    case_path.write_text(json.dumps(old_case, indent=2) + "\n")
    source_case = tmp_path / "source-case.json"
    source_case.write_bytes(case_path.read_bytes())
    archive_hash = digest(archive.read_bytes())
    proof = tmp_path / "source-replay.json"
    proof.write_text(
        json.dumps(
            {
                "status": "synthetic",
                "case_semantically_equal": True,
                "anchor": anchor,
                "anchor_cursor": anchor - 1,
                "archive_sha256_before": archive_hash,
                "archive_sha256_after": archive_hash,
                "source_case_sha256": digest(source_case.read_bytes()),
                "anchor_semantic_digest": "semantic",
                "anchor_protocol_digests": ["protocol"],
                "audit": {"validated_model_sequences": [anchor]},
            }
        )
    )
    return case_path, source_case, proof, digest(case_path.read_bytes()), digest(proof.read_bytes())


def test_frozen_case_is_derived_from_original_native_observations(tmp_path: Path) -> None:
    case, source, proof, case_hash, proof_hash = fixture(tmp_path)
    before = case.read_bytes()
    result = enrich_historical_case(
        case, source, proof, case_hash, proof_hash, tmp_path / "derived"
    )
    derived = json.loads(result.case_path.read_text())
    encounter = derived["events"][0]["state"]["fights"][0].pop("encounter")
    assert encounter == {"definition_id": "ENCOUNTER.OLD", "name": "Old enemy"}
    assert derived == json.loads(before)
    assert case.read_bytes() == before
    assert json.loads(result.provenance_path.read_text())["source_replay"] == "synthetic"


@pytest.mark.parametrize(
    "scenario", ("missing", "different_id", "different_name_without_id", "repeated", "interrupted")
)
def test_ambiguous_native_history_does_not_gain_an_encounter(tmp_path: Path, scenario: str) -> None:
    case, source, proof, case_hash, proof_hash = fixture(tmp_path, scenario)
    result = enrich_historical_case(
        case, source, proof, case_hash, proof_hash, tmp_path / "derived"
    )
    derived = json.loads(result.case_path.read_text())
    assert derived["events"][0]["state"]["fights"][0]["encounter"] is None
    assert json.loads(result.provenance_path.read_text())["fights"][0]["resolution"] == "unknown"


@pytest.mark.parametrize("scenario", ("gap", "other_run", "unobserved_write"))
def test_unreliable_archive_prefix_is_rejected(tmp_path: Path, scenario: str) -> None:
    case, source, proof, case_hash, proof_hash = fixture(tmp_path, scenario)
    output = tmp_path / "derived"
    with pytest.raises(HistoricalEnrichmentError):
        enrich_historical_case(case, source, proof, case_hash, proof_hash, output)
    assert not output.exists()


def test_post_anchor_observations_are_not_used(tmp_path: Path) -> None:
    case, source, proof, case_hash, proof_hash = fixture(tmp_path, "post_anchor")
    result = enrich_historical_case(
        case, source, proof, case_hash, proof_hash, tmp_path / "derived"
    )
    derived = json.loads(result.case_path.read_text())
    assert (
        derived["events"][0]["state"]["fights"][0]["encounter"]["definition_id"] == "ENCOUNTER.OLD"
    )


def test_wrong_case_hash_is_rejected_without_output(tmp_path: Path) -> None:
    case, source, proof, _, proof_hash = fixture(tmp_path)
    output = tmp_path / "derived"
    with pytest.raises(HistoricalEnrichmentError):
        enrich_historical_case(case, source, proof, "wrong", proof_hash, output)
    assert not output.exists()


def test_reversed_fight_history_is_not_enriched(tmp_path: Path) -> None:
    case, source, proof, case_hash, proof_hash = fixture(tmp_path, "reversed")
    result = enrich_historical_case(
        case, source, proof, case_hash, proof_hash, tmp_path / "derived"
    )
    derived = json.loads(result.case_path.read_text())
    assert all(fight["encounter"] is None for fight in derived["events"][0]["state"]["fights"])
    assert all(
        item["resolution"] == "unknown"
        for item in json.loads(result.provenance_path.read_text())["fights"]
    )


def test_changed_archive_is_rejected_before_output(tmp_path: Path) -> None:
    case, source, proof, case_hash, proof_hash = fixture(tmp_path)
    archive = Path(json.loads(case.read_text())["source"]["archive"])
    with archive.open("ab") as stream:
        stream.write(b"tampered")
    output = tmp_path / "derived"
    with pytest.raises(HistoricalEnrichmentError):
        enrich_historical_case(case, source, proof, case_hash, proof_hash, output)
    assert not output.exists()


@pytest.mark.parametrize("field", ("case_semantically_equal", "anchor_protocol_digests"))
def test_unverified_source_replay_is_rejected(tmp_path: Path, field: str) -> None:
    case, source, proof, case_hash, _ = fixture(tmp_path)
    evidence = json.loads(proof.read_text())
    evidence[field] = False if field == "case_semantically_equal" else ["different"]
    proof.write_text(json.dumps(evidence))
    output = tmp_path / "derived"
    with pytest.raises(HistoricalEnrichmentError):
        enrich_historical_case(case, source, proof, case_hash, digest(proof.read_bytes()), output)
    assert not output.exists()


def test_modified_native_blob_is_rejected_even_with_updated_archive_hash(tmp_path: Path) -> None:
    case, source, proof, case_hash, _ = fixture(tmp_path)
    archive = Path(json.loads(case.read_text())["source"]["archive"])
    with sqlite3.connect(archive) as db:
        reply = json.loads(db.execute("SELECT reply FROM records WHERE sequence = 1").fetchone()[0])
        db.execute(
            "UPDATE blobs SET content = ? WHERE digest = ?",
            (zlib.compress(b"corrupted"), reply["observations"][0]),
        )
    evidence = json.loads(proof.read_text())
    evidence["archive_sha256_before"] = digest(archive.read_bytes())
    evidence["archive_sha256_after"] = evidence["archive_sha256_before"]
    proof.write_text(json.dumps(evidence))
    output = tmp_path / "derived"
    with pytest.raises(HistoricalEnrichmentError):
        enrich_historical_case(case, source, proof, case_hash, digest(proof.read_bytes()), output)
    assert not output.exists()


def test_existing_output_is_not_overwritten(tmp_path: Path) -> None:
    case, source, proof, case_hash, proof_hash = fixture(tmp_path)
    output = tmp_path / "derived"
    output.mkdir()
    marker = output / "case.json"
    marker.write_text("existing")
    with pytest.raises(FileExistsError):
        enrich_historical_case(case, source, proof, case_hash, proof_hash, output)
    assert marker.read_text() == "existing"


def test_different_source_task_cannot_be_bound_to_the_frozen_case(tmp_path: Path) -> None:
    case, source, proof, case_hash, _ = fixture(tmp_path)
    original = json.loads(source.read_text())
    original["events"][0]["observation"]["location"]["node_id"] = "different-node"
    source.write_text(json.dumps(original))
    evidence = json.loads(proof.read_text())
    evidence["source_case_sha256"] = digest(source.read_bytes())
    proof.write_text(json.dumps(evidence))
    output = tmp_path / "derived"
    with pytest.raises(HistoricalEnrichmentError):
        enrich_historical_case(case, source, proof, case_hash, digest(proof.read_bytes()), output)
    assert not output.exists()
