from pathlib import Path

from sts2_harness.journal import Journal, Manifest
from sts2_harness_eval.prune import ArchivePruner, PrunePlan, PruneRequest, PruneScope, RunRef


def archive(path: Path, identity: str) -> RunRef:
    journal = Journal.create(
        path,
        Manifest(recording_id=identity, core_version="test", game_instance_id="game", run_id="run"),
    )
    journal.close()
    return RunRef(path, identity)


def test_inspection_only_lists_explicit_unreferenced_archives(tmp_path: Path) -> None:
    selected = archive(tmp_path / "selected.sqlite", "selected")
    retained = archive(tmp_path / "retained.sqlite", "retained")
    before = {run.archive: run.archive.read_bytes() for run in (selected, retained)}
    result = ArchivePruner().inspect(
        PruneScope(archives=(selected.archive, retained.archive), complete=True, offline=True),
        PruneRequest(archives=(selected,)),
    )
    assert isinstance(result, PrunePlan)
    assert result.requested == (selected,)
    assert result.removable == (selected,)
    assert result.protected == ()
    assert {path: path.read_bytes() for path in before} == before


def test_cases_protect_branches_and_whole_transitive_parents(tmp_path: Path) -> None:
    from hashlib import sha256

    from test_annotations import cases_fixture
    from test_responses import configuration

    from sts2_harness.client import models as native
    from sts2_harness.journal import ParentPrefix
    from sts2_harness.journal_models import ConfigurationSnapshot, ToolSet
    from sts2_harness_eval.prune import CaseDependency
    from sts2_harness_eval.results import ModelAnchor

    store = cases_fixture(tmp_path / "store")
    case = store.load("battle")
    parent = RunRef(case.source.archive, "source")
    manifest = Journal.read_manifest(parent.archive)
    branch = RunRef(tmp_path / "branch.sqlite", "branch")
    journal = Journal.create(
        branch.archive,
        manifest.model_copy(
            update={
                "recording_id": branch.recording_id,
                "parent": ParentPrefix(
                    archive=parent.archive,
                    recording_id="source",
                    before_sequence=1,
                    archive_sha256=sha256(parent.archive.read_bytes()).hexdigest(),
                ),
            }
        ),
    )
    sequence = journal.begin_model(
        native.DecisionOwner.COMBAT,
        "combat",
        (),
        ToolSet(tools=()),
        ConfigurationSnapshot.of(configuration(tmp_path)),
    )
    journal.finish_model(sequence)
    journal.end_model()
    journal.close()
    branch_case = case.model_copy(
        update={
            "id": "branch-case",
            "source": case.source.model_copy(
                update={"archive": branch.archive, "anchor": ModelAnchor("branch", 1)}
            ),
        }
    )
    store.publish(branch_case)
    unrelated = archive(tmp_path / "unrelated.sqlite", "unrelated")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = ArchivePruner().inspect(
        PruneScope(
            archives=(parent.archive, branch.archive, unrelated.archive),
            complete=True,
            offline=True,
            case_stores=(store.directory,),
        ),
        PruneRequest(archives=(parent, branch, unrelated)),
    )
    assert isinstance(result, PrunePlan)
    assert result.removable == (unrelated,)
    assert tuple(item.run for item in result.protected) == (parent, branch)
    parent_chains = result.protected[0].reasons
    chain = next(
        reason
        for reason in parent_chains
        if isinstance(reason.root, CaseDependency) and reason.root.case_id == "branch-case"
    )
    assert isinstance(chain.root, CaseDependency)
    assert chain.root.source == branch
    assert [(edge.child, edge.parent) for edge in chain.parents] == [(branch, parent)]
    assert chain.parents[0].before_sequence == 1
    assert chain.parents[0].archive_sha256 == sha256(before[parent.archive]).hexdigest()
    assert {item.path for item in result.shared} >= {
        store.directory / "checkpoints" / case.checkpoint.sha256 / "current_run.save",
        store.directory / "bindings" / (case.binding + ".json"),
        store.directory / "evidence" / (case.evidence + ".json"),
    }
    assert {path: path.read_bytes() for path in before} == before


def test_unreferenced_children_precede_selected_parents(tmp_path: Path) -> None:
    from hashlib import sha256

    from test_responses import configuration

    from sts2_harness.client import models as native
    from sts2_harness.journal import ParentPrefix
    from sts2_harness.journal_models import ConfigurationSnapshot, ToolSet

    parent = RunRef(tmp_path / "parent.sqlite", "parent")
    manifest = Manifest(
        recording_id="parent", core_version="test", game_instance_id="game", run_id="run"
    )
    journal = Journal.create(parent.archive, manifest)
    sequence = journal.begin_model(
        native.DecisionOwner.RUN,
        "run",
        (),
        ToolSet(tools=()),
        ConfigurationSnapshot.of(configuration(tmp_path)),
    )
    journal.finish_model(sequence)
    journal.end_model()
    journal.close()
    child = RunRef(tmp_path / "child.sqlite", "child")
    journal = Journal.create(
        child.archive,
        manifest.model_copy(
            update={
                "recording_id": "child",
                "parent": ParentPrefix(
                    archive=parent.archive,
                    recording_id="parent",
                    before_sequence=sequence,
                    archive_sha256=sha256(parent.archive.read_bytes()).hexdigest(),
                ),
            }
        ),
    )
    journal.close()
    scope = PruneScope(archives=(parent.archive, child.archive), complete=True, offline=True)
    result = ArchivePruner().inspect(scope, PruneRequest(archives=(parent, child)))
    assert isinstance(result, PrunePlan)
    assert result.removable == (child, parent)
    assert result.protected == ()
    kept_child = ArchivePruner().inspect(scope, PruneRequest(archives=(parent,)))
    assert isinstance(kept_child, PrunePlan)
    assert kept_child.removable == ()
    assert kept_child.protected[0].reasons[0].root == child


def test_incomplete_or_damaged_declared_scope_refuses_the_entire_plan(tmp_path: Path) -> None:
    from dataclasses import replace

    from sts2_harness_eval.prune import PruneRefused

    selected = archive(tmp_path / "selected.sqlite", "selected")
    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"not an archive")
    original = selected.archive.read_bytes()
    scope = PruneScope(archives=(selected.archive,), complete=True, offline=True)
    request = PruneRequest(archives=(selected,))
    missing_store = tmp_path / "missing-store"
    invalid_scopes = (
        replace(scope, complete=False),
        replace(scope, offline=False),
        replace(scope, archives=(selected.archive, broken)),
        replace(scope, archives=(selected.archive, tmp_path / "missing.sqlite")),
        replace(scope, case_stores=(missing_store,)),
        replace(scope, template_roots=(tmp_path / "missing-templates",)),
    )
    for invalid in invalid_scopes:
        result = ArchivePruner().inspect(invalid, request)
        assert isinstance(result, PruneRefused), result
        assert result.message
    wrong_identity = ArchivePruner().inspect(
        scope, PruneRequest(archives=(RunRef(selected.archive, "other"),))
    )
    assert isinstance(wrong_identity, PruneRefused)
    assert selected.archive.read_bytes() == original


def test_visible_external_parents_cycles_and_unreadable_files_are_refused(tmp_path: Path) -> None:
    import sqlite3
    from hashlib import sha256

    from sts2_harness.journal import ParentPrefix
    from sts2_harness_eval.prune import PruneRefused

    first = archive(tmp_path / "first.sqlite", "first")
    second = archive(tmp_path / "second.sqlite", "second")
    first_manifest = Journal.read_manifest(first.archive)
    second_manifest = Journal.read_manifest(second.archive)
    first_parent = ParentPrefix(
        archive=second.archive,
        recording_id="second",
        before_sequence=1,
        archive_sha256=sha256(second.archive.read_bytes()).hexdigest(),
    )

    def replace_manifest(run: RunRef, manifest: Manifest) -> None:
        # Corrupt external archive input at the storage boundary, without bypassing inspect.
        with sqlite3.connect(run.archive) as connection:
            connection.execute("UPDATE manifest SET value=?", (manifest.model_dump_json(),))

    replace_manifest(first, first_manifest.model_copy(update={"parent": first_parent}))
    request = PruneRequest(archives=(first,))
    outside = ArchivePruner().inspect(
        PruneScope(archives=(first.archive,), complete=True, offline=True), request
    )
    assert isinstance(outside, PruneRefused)
    assert "outside scope" in outside.message
    second_parent = ParentPrefix(
        archive=first.archive,
        recording_id="first",
        before_sequence=1,
        archive_sha256=sha256(first.archive.read_bytes()).hexdigest(),
    )
    replace_manifest(second, second_manifest.model_copy(update={"parent": second_parent}))
    scope = PruneScope(archives=(first.archive, second.archive), complete=True, offline=True)
    cycle = ArchivePruner().inspect(scope, request)
    assert isinstance(cycle, PruneRefused)
    assert "cycle" in cycle.message
    first.archive.chmod(0)
    try:
        unreadable = ArchivePruner().inspect(scope, request)
        assert isinstance(unreadable, PruneRefused)
        assert "read" in unreadable.message.lower()
    finally:
        first.archive.chmod(0o600)


def test_retained_templates_are_resolved_only_inside_declared_roots(tmp_path: Path) -> None:
    from test_headless import game_fixture

    from sts2_harness.native_template import NativeTemplate
    from sts2_harness_eval.prune import PruneRefused, SharedKind

    _, template_path = game_fixture(tmp_path)
    template = NativeTemplate(template_path)
    first_root = tmp_path / "templates-one"
    second_root = tmp_path / "templates-two"
    template.copy_to(tmp_path / "profile-one", first_root)
    template.copy_to(tmp_path / "profile-two", second_root)
    key = template.evidence.manifest_sha256
    retained = RunRef(tmp_path / "retained.sqlite", "retained")
    Journal.create(
        retained.archive,
        Manifest(
            recording_id="retained",
            core_version="test",
            game_instance_id="game",
            run_id="run",
            template=key,
        ),
    ).close()
    selected = archive(tmp_path / "selected.sqlite", "selected")
    scope = PruneScope(
        archives=(selected.archive, retained.archive),
        complete=True,
        offline=True,
        template_roots=(first_root, second_root),
    )
    request = PruneRequest(archives=(selected,))
    result = ArchivePruner().inspect(scope, request)
    assert isinstance(result, PrunePlan)
    assert result.removable == (selected,)
    assert [(item.kind, item.path, item.sha256, item.owner) for item in result.shared] == [
        (SharedKind.TEMPLATE, first_root / key, key, retained),
        (SharedKind.TEMPLATE, second_root / key, key, retained),
    ]
    from dataclasses import replace

    outside = ArchivePruner().inspect(replace(scope, template_roots=()), request)
    assert isinstance(outside, PruneRefused)
    assert "template" in outside.message.lower()


def test_manifest_without_journal_indexes_does_not_count_as_a_valid_archive(tmp_path: Path) -> None:
    import sqlite3

    from sts2_harness_eval.prune import PruneRefused

    selected = archive(tmp_path / "selected.sqlite", "selected")
    with sqlite3.connect(selected.archive) as connection:
        connection.execute("DROP TABLE summaries")
    result = ArchivePruner().inspect(
        PruneScope(archives=(selected.archive,), complete=True, offline=True),
        PruneRequest(archives=(selected,)),
    )
    assert isinstance(result, PruneRefused)
