"""Verify a historical decision using its source packages and a compatible extractor."""

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import socket
import sqlite3
import sys
import zlib
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Never

sys.dont_write_bytecode = True


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_digest(path: Path) -> str:
    result = hashlib.sha256()
    for item in sorted(path.rglob("*.py")):
        result.update(str(item.relative_to(path)).encode())
        result.update(b"\0")
        result.update(item.read_bytes())
        result.update(b"\0")
    return result.hexdigest()


def archive_wal_clear(archive: Path) -> bool:
    wal = Path(str(archive) + "-wal")
    return not wal.exists() or wal.stat().st_size == 0


def deny_network(*args: object, **kwargs: object) -> Never:
    raise RuntimeError("Historical source replay does not allow network requests.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-case", type=Path, required=True)
    parser.add_argument("--extractor-directory", type=Path, required=True)
    parser.add_argument("--provider-module")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    import sts2_harness
    import sts2_harness_eval

    sts2_harness_eval.__path__.insert(0, str(args.extractor_directory))
    from sts2_harness.entry_materials import MaterialContext
    from sts2_harness.journal import ArchiveError, Journal
    from sts2_harness_eval._replay import ReplayRejected
    from sts2_harness_eval.decision_cases import DecisionCase, TaskEvent
    from sts2_harness_eval.decision_extract import ExtractionAudit, extract_case

    source_bytes = args.source_case.read_bytes()
    source_case = DecisionCase.model_validate_json(source_bytes)
    task = next(
        (event for event in reversed(source_case.events) if isinstance(event, TaskEvent)), None
    )
    if task is None:
        raise ValueError("The source case has no task event.")
    archive = source_case.source.archive
    archive_before = digest(archive)
    audit = ExtractionAudit()
    public_path = Path(sts2_harness.__file__).parent
    extractor = Path(extract_case.__code__.co_filename)
    report: dict[str, object] = {
        "status": "failed",
        "source_public_module": sts2_harness.__file__,
        "source_public_python_sha256": package_digest(public_path),
        "extractor": str(extractor),
        "extractor_sha256": digest(extractor),
        "replay_script_sha256": digest(Path(__file__)),
        "python": sys.version,
        "pydantic_version": importlib.metadata.version("pydantic"),
        "archive_sha256_before": archive_before,
        "archive_wal_clear_before": archive_wal_clear(archive),
        "source_case_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "anchor": source_case.source.model_sequence,
        "network": "socket.connect and connect_ex denied",
    }
    error: Exception | None = None
    try:
        if not report["archive_wal_clear_before"]:
            raise ValueError("The archive has a nonempty WAL before replay.")
        if extractor.resolve() != (args.extractor_directory / "decision_extract.py").resolve():
            raise ValueError("The imported extractor differs from the selected source.")
        documents = None
        if args.provider_module:
            provider = importlib.import_module(args.provider_module)
            provider_file = getattr(provider, "__file__", None)
            if provider_file is None:
                raise ValueError("The source provider has no file path.")
            report["source_private_module"] = provider_file
            report["source_private_python_sha256"] = package_digest(Path(provider_file).parent)
            if not callable(getattr(provider, "entry_materials", None)):
                raise ValueError("The source provider has no callable entry_materials.")
            source_materials = provider.entry_materials()
            if not callable(getattr(source_materials, "documents", None)):
                raise ValueError("The source provider has no callable documents capability.")
            documents = source_materials.documents(
                MaterialContext(task.observation.run_rules.ascension)
            )
        derived = asyncio.run(
            extract_case(
                source_case.source, source_case.labels, source_documents=documents, audit=audit
            )
        )
        report["case_semantically_equal"] = derived == source_case
        if derived != source_case:
            raise ValueError("The reconstructed source case differs from the supplied case.")
        with closing(Journal.open(archive)) as journal:
            anchor = journal.request(source_case.source.model_sequence)
            report["anchor_semantic_digest"] = anchor.semantic_digest
            report["anchor_protocol_digests"] = [
                item.protocol_encoding_digest for item in anchor.attempts
            ]
            report["anchor_cursor"] = anchor.confirmed_operation_cursor
        if source_case.source.model_sequence not in audit.validated_model_sequences:
            raise ValueError("Source replay did not validate the requested model anchor.")
        report["status"] = "verified"
    except Exception as failure:
        error = failure
    report["audit"] = asdict(audit)
    try:
        report["archive_sha256_after"] = digest(archive)
        report["archive_wal_clear_after"] = archive_wal_clear(archive)
        if (
            report["archive_sha256_after"] != archive_before
            or args.source_case.read_bytes() != source_bytes
        ):
            report["status"] = "failed"
            if error is None:
                error = ValueError("The source archive or case changed during replay.")
        if not report["archive_wal_clear_after"]:
            report["status"] = "failed"
            if error is None:
                error = ValueError("The archive has a nonempty WAL after replay.")
    except OSError as failure:
        report["status"] = "failed"
        report["postcheck_error_type"] = type(failure).__name__
        if error is None:
            error = failure
    if error is not None:
        report["error_type"] = type(error).__name__
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if error is not None:
        if isinstance(error, (ArchiveError, ReplayRejected)):
            raise ValueError(f"Source replay failed: {type(error).__name__}.") from error
        raise error


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, ImportError, sqlite3.Error, zlib.error) as error:
        detail = str(error).partition("\n")[0]
        print(f"Source replay error: {type(error).__name__}: {detail}", file=sys.stderr)
        raise SystemExit(2) from None
