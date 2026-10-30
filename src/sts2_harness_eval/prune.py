"""Inspect retention within a caller-declared complete, offline archive scope.

Metadata inspection does not prove the integrity of archived model or game bodies.
Only explicitly requested whole archives can be removed. Shared contents remain retained.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from sts2_harness.journal import ArchiveError, Journal, Manifest, SceneVariant
from sts2_harness.native_template import Sha256, TemplateManifest, digest
from sts2_harness_eval.cases import Case, CaseStore


@dataclass(frozen=True)
class RunRef:
    archive: Path
    recording_id: str


@dataclass(frozen=True)
class PruneScope:
    """All relevant archives, stores and template roots, with producers stopped."""

    archives: tuple[Path, ...]
    complete: bool
    offline: bool
    case_stores: tuple[Path, ...] = ()
    template_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class PruneRequest:
    archives: tuple[RunRef, ...]


@dataclass(frozen=True)
class CaseDependency:
    store: Path
    case_id: str
    source: RunRef


@dataclass(frozen=True)
class ParentDependency:
    child: RunRef
    parent: RunRef
    before_sequence: int
    archive_sha256: str


@dataclass(frozen=True)
class SceneSourceDependency:
    child: RunRef
    source: RunRef
    model_sequence: int
    archive_sha256: str

    @property
    def parent(self) -> RunRef:
        """The retained source is a dependency, without an inherited software prefix."""
        return self.source


@dataclass(frozen=True)
class Protection:
    root: RunRef | CaseDependency
    parents: tuple[ParentDependency | SceneSourceDependency, ...]


@dataclass(frozen=True)
class ProtectedArchive:
    run: RunRef
    reasons: tuple[Protection, ...]


class SharedKind(StrEnum):
    CHECKPOINT = "checkpoint"
    BINDING = "binding"
    REFERENCE = "reference"
    EVIDENCE = "evidence"
    TEMPLATE = "template"


@dataclass(frozen=True)
class SharedDependency:
    kind: SharedKind
    path: Path
    sha256: str
    owner: RunRef | CaseDependency


@dataclass(frozen=True)
class PrunePlan:
    requested: tuple[RunRef, ...]
    removable: tuple[RunRef, ...]
    protected: tuple[ProtectedArchive, ...]
    shared: tuple[SharedDependency, ...]


@dataclass(frozen=True)
class PruneRefused:
    message: str
    protected: tuple[ProtectedArchive, ...] = ()


@dataclass(frozen=True)
class PruneCompleted:
    removed: tuple[RunRef, ...]
    bytes: int


@dataclass(frozen=True)
class PruneIncomplete:
    removed: tuple[RunRef, ...]
    bytes: int
    failed: RunRef
    remaining: tuple[RunRef, ...]
    message: str


class ArchivePruner:
    def prune(
        self, scope: PruneScope, request: PruneRequest
    ) -> PruneCompleted | PruneRefused | PruneIncomplete:
        """Recheck current offline relations before deleting selected whole archives."""
        plan = self.inspect(scope, request)
        if isinstance(plan, PruneRefused):
            return plan
        if plan.protected:
            return PruneRefused("The request includes protected archives.", plan.protected)
        try:
            sizes = tuple(run.archive.stat().st_size for run in plan.removable)
        except OSError as error:
            return PruneRefused(str(error))
        removed: list[RunRef] = []
        total_bytes = 0
        for index, (run, size) in enumerate(zip(plan.removable, sizes, strict=True)):
            try:
                run.archive.unlink()
            except OSError as error:
                return PruneIncomplete(
                    tuple(removed), total_bytes, run, plan.removable[index:], str(error)
                )
            removed.append(run)
            total_bytes += size
        return PruneCompleted(tuple(removed), total_bytes)

    def inspect(self, scope: PruneScope, request: PruneRequest) -> PrunePlan | PruneRefused:
        """Return a read-only plan, never an authorization token for later deletion."""
        try:
            return self._inspect(scope, request)
        except (OSError, ArchiveError, ValidationError) as error:
            return PruneRefused(str(error))

    def _inspect(self, scope: PruneScope, request: PruneRequest) -> PrunePlan:
        if not scope.complete or not scope.offline:
            raise ArchiveError("Retention inspection requires a complete offline scope.")
        paths = tuple(self._file(path) for path in scope.archives)
        for path in paths:
            Journal.archive_sha256(path)
        manifests = {path: Journal.read_manifest(path) for path in paths}
        runs = {path: RunRef(path, manifest.recording_id) for path, manifest in manifests.items()}
        requested = tuple(
            RunRef(self._file(run.archive), run.recording_id) for run in request.archives
        )
        if len(set(requested)) != len(requested):
            raise ArchiveError("Each requested archive must be selected only once.")
        for run in requested:
            if runs.get(run.archive) != run:
                raise ArchiveError(
                    "The requested archive identity is outside scope or has changed."
                )
        parents = self._parents(manifests, runs)
        roots: list[RunRef | CaseDependency] = [
            run for run in runs.values() if run not in requested
        ]
        shared: list[SharedDependency] = []
        for directory in scope.case_stores:
            self._cases(directory, runs, roots, shared)
        reasons: dict[RunRef, list[Protection]] = {}
        for root in roots:
            run = root.source if isinstance(root, CaseDependency) else root
            chain: tuple[ParentDependency | SceneSourceDependency, ...] = ()
            while True:
                reasons.setdefault(run, []).append(Protection(root, chain))
                edge = parents.get(run)
                if edge is None:
                    break
                chain += (edge,)
                run = edge.parent
        self._templates(scope.template_roots, manifests, tuple(runs.values()), shared)
        removable = [run for run in requested if run not in reasons]

        # Depth places every descendant before its ancestors.
        def depth(run: RunRef) -> int:
            count = 0
            while run in parents:
                count += 1
                run = parents[run].parent
            return count

        removable.sort(key=depth, reverse=True)
        return PrunePlan(
            requested,
            tuple(removable),
            tuple(
                ProtectedArchive(run, tuple(reasons[run])) for run in requested if run in reasons
            ),
            tuple(shared),
        )

    @staticmethod
    def _file(path: Path) -> Path:
        if path.is_symlink() or not path.is_file():
            raise ArchiveError(f"A declared dependency is missing or is not a regular file: {path}")
        return path.resolve(strict=True)

    @staticmethod
    def _directory(path: Path) -> Path:
        if path.is_symlink() or not path.is_dir():
            raise ArchiveError(f"A declared directory is missing or invalid: {path}")
        # Enumerate even empty roots so unreadable directories cannot imply zero references.
        tuple(path.iterdir())
        return path.resolve(strict=True)

    def _parents(
        self, manifests: dict[Path, Manifest], runs: dict[Path, RunRef]
    ) -> dict[RunRef, ParentDependency | SceneSourceDependency]:
        parents: dict[RunRef, ParentDependency | SceneSourceDependency] = {}
        for path, manifest in manifests.items():
            if isinstance(manifest.variant, SceneVariant):
                source = manifest.variant.source
                source_path = self._file(source.archive)
                source_run = runs.get(source_path)
                if source_run is None or source_run.recording_id != source.recording_id:
                    raise ArchiveError(
                        "A Scene source archive is outside scope or its identity has changed."
                    )
                parents[runs[path]] = SceneSourceDependency(
                    runs[path], source_run, source.model_sequence, source.archive_sha256
                )
                continue
            prefix = manifest.parent
            if prefix is None:
                continue
            parent_path = self._file(prefix.archive)
            parent = runs.get(parent_path)
            if parent is None or parent.recording_id != prefix.recording_id:
                raise ArchiveError("A parent archive is outside scope or its identity has changed.")
            parents[runs[path]] = ParentDependency(
                runs[path], parent, prefix.before_sequence, prefix.archive_sha256
            )
        for start in runs.values():
            visited: set[RunRef] = set()
            run = start
            while run in parents:
                if run in visited:
                    raise ArchiveError("Archive parent relations contain a cycle.")
                visited.add(run)
                run = parents[run].parent
        for edge in parents.values():
            if Journal.archive_sha256(edge.parent.archive) != edge.archive_sha256:
                raise ArchiveError("A whole archive dependency hash has changed.")
        return parents

    def _cases(
        self,
        directory: Path,
        runs: dict[Path, RunRef],
        roots: list[RunRef | CaseDependency],
        shared: list[SharedDependency],
    ) -> None:
        store = CaseStore(self._directory(directory))
        case_directory = store.directory / "cases"
        if not case_directory.exists() and not case_directory.is_symlink():
            return
        self._directory(case_directory)
        for path in sorted(case_directory.iterdir()):
            if path.suffix != ".json":
                continue
            case = Case.model_validate_json(self._file(path).read_bytes())
            if case.id != path.stem:
                raise ArchiveError("A case file identity has changed.")
            source = runs.get(self._file(case.source.archive))
            if source is None or source.recording_id != case.source.anchor.recording_id:
                raise ArchiveError("A case source is outside scope or its identity has changed.")
            dependency = CaseDependency(store.directory, case.id, source)
            roots.append(dependency)
            checkpoint_path = self._file(
                store.directory / "checkpoints" / case.checkpoint.sha256 / "current_run.save"
            )
            binding = store.binding(case)
            store.reference(binding)
            store.checkpoint_path(case.checkpoint)
            shared.append(
                SharedDependency(
                    SharedKind.CHECKPOINT,
                    checkpoint_path,
                    case.checkpoint.sha256,
                    dependency,
                )
            )
            for kind, key in (
                (SharedKind.BINDING, case.binding),
                (SharedKind.REFERENCE, binding.reference),
                (SharedKind.EVIDENCE, case.evidence),
            ):
                content_path = self._file(
                    store.directory
                    / (kind.value + "s" if kind is not SharedKind.EVIDENCE else "evidence")
                    / (key + ".json")
                )
                if digest(content_path) != key:
                    raise ArchiveError("A shared case dependency has changed.")
                shared.append(SharedDependency(kind, content_path, key, dependency))

    def _templates(
        self,
        directories: tuple[Path, ...],
        manifests: dict[Path, Manifest],
        runs: tuple[RunRef, ...],
        shared: list[SharedDependency],
    ) -> None:
        roots = tuple(self._directory(path) for path in directories)
        for run in runs:
            key = manifests[run.archive].template
            if key is None:
                continue
            key = TypeAdapter(Sha256).validate_python(key)
            found = False
            for root in roots:
                directory = root / key
                if not directory.exists():
                    continue
                self._directory(directory)
                manifest_path = self._file(directory / "manifest.json")
                if digest(manifest_path) != key:
                    raise ArchiveError("A retained template manifest has changed.")
                manifest = TemplateManifest.model_validate_json(manifest_path.read_bytes())
                for item in manifest.files:
                    path = self._file(directory / item.path)
                    if not path.is_relative_to(directory) or digest(path) != item.sha256:
                        raise ArchiveError("A retained template file has changed.")
                shared.append(SharedDependency(SharedKind.TEMPLATE, directory, key, run))
                found = True
            if not found:
                raise ArchiveError("A retained template is outside the declared template roots.")
