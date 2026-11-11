from hashlib import sha256
from pathlib import Path

from sts2_harness.capacity import CapacityPolicy
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness_eval.prune import ArchivePruner, PrunePlan, PruneRequest, PruneScope, RunRef


def archive(path: Path, identity: str) -> RunRef:
    journal = Journal.create(
        path,
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id=identity,
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    journal.close()
    return RunRef(path, identity)


def test_cases_protect_branches_and_whole_transitive_parents(tmp_path: Path) -> None:

    from test_annotations import cases_fixture
    from test_responses import configuration

    from sts2_harness.client import models as native
    from sts2_harness.journal import ParentPrefix
    from sts2_harness.journal_models import ConfigurationSnapshot, ToolSet
    from sts2_harness_eval.prune import CaseDependency, ParentDependency
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
        ConfigurationSnapshot.of(configuration(tmp_path), CapacityPolicy()),
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
    assert isinstance(chain.parents[0], ParentDependency)
    assert chain.parents[0].before_sequence == 1
    assert chain.parents[0].archive_sha256 == sha256(before[parent.archive]).hexdigest()
    assert {item.path for item in result.shared} >= {
        store.directory / "checkpoints" / case.checkpoint.sha256 / "current_run.save",
        store.directory / "bindings" / (case.binding + ".json"),
        store.directory / "evidence" / (case.evidence + ".json"),
    }
    assert {path: path.read_bytes() for path in before} == before

    from contextlib import closing

    from sts2_harness.journal import evidence
    from sts2_harness_eval.prune import PruneCompleted, PruneRefused

    scope = PruneScope(
        archives=(parent.archive, branch.archive, unrelated.archive),
        complete=True,
        offline=True,
        case_stores=(store.directory,),
    )
    refused = ArchivePruner().prune(scope, PruneRequest(archives=(unrelated, parent, branch)))
    assert isinstance(refused, PruneRefused)
    assert refused.protected == result.protected
    assert {path: path.read_bytes() for path in before} == before
    removed = ArchivePruner().prune(scope, PruneRequest(archives=(unrelated,)))
    assert isinstance(removed, PruneCompleted)
    assert removed.removed == (unrelated,)
    assert store.load("battle") == case
    assert store.load("branch-case") == branch_case
    with closing(Journal.open(branch.archive)) as reader:
        exported = evidence(reader, expand=True)
        assert exported.manifest.recording_id == "branch"
        assert len(exported.records) == 1
    assert {path: path.read_bytes() for path in before if path != unrelated.archive} == {
        path: content for path, content in before.items() if path != unrelated.archive
    }


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


def test_prune_removes_only_the_requested_archive_and_reports_actual_bytes(tmp_path: Path) -> None:
    from sts2_harness_eval.prune import PruneCompleted

    selected = archive(tmp_path / "selected.sqlite", "selected")
    import asyncio

    from test_lifecycle import Campaign
    from test_replay import record_campaign

    from sts2_harness_eval import Reached, reach

    kept = RunRef(tmp_path / "kept.sqlite", "recording")
    anchors = asyncio.run(record_campaign(kept.archive, Campaign()))
    diagnostic = tmp_path / "events.jsonl"
    save = tmp_path / "current_run.save"
    diagnostic.write_text("synthetic diagnostic")
    save.write_bytes(b"synthetic native save")
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    result = ArchivePruner().prune(
        PruneScope(archives=(selected.archive, kept.archive), complete=True, offline=True),
        PruneRequest(archives=(selected,)),
    )
    assert isinstance(result, PruneCompleted)
    assert result.removed == (selected,)
    assert result.bytes == len(before[selected.archive])
    assert not selected.archive.exists()
    assert {path: path.read_bytes() for path in before if path != selected.archive} == {
        path: content for path, content in before.items() if path != selected.archive
    }

    async def replay_retained_history() -> None:
        async with reach(kept.archive, anchors[0]) as reached:
            assert isinstance(reached, Reached)
            assert reached.anchor == anchors[0]

    asyncio.run(replay_retained_history())
