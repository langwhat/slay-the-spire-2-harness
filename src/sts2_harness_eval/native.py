"""Own independent native replay and loaded-target lifetimes for evaluation."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.game import LiveGame, unwrap
from sts2_harness.headless import (
    HeadlessLaunchMode,
    HeadlessOptions,
    OwnedHeadlessInstance,
    StandardRunRecipe,
)
from sts2_harness.native_template import NativeTemplate
from sts2_harness_eval.cases import (
    AdmissionFailure,
    AdmissionPlan,
    AdmissionRequest,
    Case,
    CaseStore,
    CheckpointRef,
    ReplayWrite,
    ShortcutBinding,
    Unsupported,
    UnsupportedCause,
    plan_admission,
)
from sts2_harness_eval.checkpoints import CheckpointClient, CheckpointUncertain
from sts2_harness_eval.verification import (
    AllowedMapDifference,
    KeyedRefs,
    LoadedCorrespondence,
    Publication,
    Reference,
    SourceCorrespondence,
    VerificationFailure,
    bind,
    loaded_refs,
    translated_step,
)


@dataclass(frozen=True)
class NativeLaunch:
    game: Path
    template_manifest: Path
    output: Path
    port: int | None = None
    startup_timeout_seconds: float = 90.0

    def options(self, recipe: StandardRunRecipe) -> HeadlessOptions:
        return HeadlessOptions(
            game=self.game,
            template_manifest=self.template_manifest,
            output=self.output,
            port=self.port,
            recipe=recipe,
            startup_timeout_seconds=self.startup_timeout_seconds,
        )


@dataclass(frozen=True)
class CapturedReach:
    checkpoint: CheckpointRef
    save: Path
    reference: Reference
    suffix: tuple[KeyedRefs, ...]
    target: KeyedRefs
    follow_on: tuple[KeyedRefs, ...]


@dataclass(frozen=True)
class LoadedTarget:
    game: LiveGame
    client: ModClient
    checkpoints: CheckpointClient
    observation: native.Observation
    differences: tuple[AllowedMapDifference, ...]
    correspondence: LoadedCorrespondence


def _route(observation: native.Observation) -> native.RouteKey:
    return native.RouteKey.model_validate(observation.location.model_dump())


def _live(client: ModClient, observation: native.Observation) -> LiveGame:
    route = _route(observation)
    return LiveGame(
        client,
        namespace=uuid4().hex,
        game_instance_id=route.game_instance_id,
        run_id=route.run_id,
        settle_confirmation_seconds=30.0,
    )


async def _control(client: ModClient, mode: native.ControlIntent) -> None:
    reply = await client.control(mode)
    if not isinstance(reply, native.ControlReply) or reply.mode is not mode:
        raise CheckpointUncertain("The evaluation owner could not verify control intent.")


async def _write(
    game: LiveGame, client: ModClient, before: native.Observation, step: native.NativeStep
) -> None:
    await _control(client, native.ControlIntent.RUNNING)
    settlement = unwrap(await game.settle(_route(before), step))
    if not isinstance(settlement.receipt, native.ReceiptCompleted):
        raise CheckpointUncertain("The original confirmed write did not complete in the new run.")
    await _control(client, native.ControlIntent.PAUSED)


@dataclass(frozen=True)
class _BoundPublication:
    binding: KeyedRefs
    publication: Publication
    source_refs: dict[str, str]


@dataclass(frozen=True)
class _DerivedPath:
    bindings: tuple[KeyedRefs, ...]
    before: tuple[Publication, ...]
    after: tuple[Publication, ...]


class _CaptureReplay:
    def __init__(self, client: ModClient, checkpoints: CheckpointClient, game: LiveGame) -> None:
        self.client = client
        self.checkpoints = checkpoints
        self.game = game
        self.correspondence = SourceCorrespondence()

    async def prefix(self, writes: tuple[ReplayWrite, ...]) -> None:
        for write in writes:
            read = await self.checkpoints.identity()
            refs = self.correspondence.compare(write.before, read.observation, write.sequence)
            await _write(
                self.game, self.client, read.observation, translated_step(write.step, refs)
            )

    async def publication(self, source: native.Observation, sequence: int) -> _BoundPublication:
        read = await self.checkpoints.identity()
        refs = self.correspondence.compare(source, read.observation, sequence)
        try:
            binding = bind(sequence, source, read, refs)
        except VerificationFailure as error:
            error.sequence = sequence
            raise
        publication = Publication(source_sequence=sequence, source_stamp=source.stamp, read=read)
        return _BoundPublication(binding, publication, refs)

    async def path(self, writes: tuple[ReplayWrite, ...], *, verify_after: bool) -> _DerivedPath:
        bindings: list[KeyedRefs] = []
        before: list[Publication] = []
        after: list[Publication] = []
        for write in writes:
            current = await self.publication(write.before, write.sequence)
            bindings.append(current.binding)
            before.append(current.publication)
            await _write(
                self.game,
                self.client,
                current.publication.read.observation,
                translated_step(write.step, current.source_refs),
            )
            if verify_after:
                following = await self.publication(write.after, write.sequence)
                after.append(following.publication)
        return _DerivedPath(tuple(bindings), tuple(before), tuple(after))


class NativeRestorer:
    def __init__(self, launch: NativeLaunch, store: CaseStore) -> None:
        self.launch = launch
        self.store = store

    async def capture(
        self, plan: AdmissionPlan, existing: CheckpointRef | None = None
    ) -> CapturedReach:
        recipe = self._launch_recipe(plan)
        async with OwnedHeadlessInstance(
            self.launch.options(recipe), development_checkpoints=True
        ) as owner:
            if owner.receipt.seed != plan.manifest.seed:
                raise AdmissionFailure(
                    Unsupported(cause=UnsupportedCause.INITIAL_CONDITIONS, sequence=None)
                )
            url = f"http://127.0.0.1:{owner.port}"
            async with ModClient(url) as client, CheckpointClient(url) as checkpoints:
                replay = _CaptureReplay(client, checkpoints, _live(client, owner.opening))
                await replay.prefix(plan.prefix)
                boundary = await replay.publication(plan.boundary, plan.boundary_sequence)
                checkpoint, save = await self._checkpoint(plan, checkpoints, boundary, existing)
                suffix = await replay.path(plan.suffix, verify_after=False)
                target = await replay.publication(plan.target, plan.source.anchor.model_sequence)
                follow_on = await replay.path(plan.follow_on, verify_after=True)
        return CapturedReach(
            checkpoint,
            save,
            Reference(
                boundary=boundary.publication,
                suffix=suffix.before,
                target=target.publication,
                follow_on_before=follow_on.before,
                follow_on=follow_on.after,
            ),
            suffix.bindings,
            target.binding,
            follow_on.bindings,
        )

    async def _checkpoint(
        self,
        plan: AdmissionPlan,
        checkpoints: CheckpointClient,
        boundary: _BoundPublication,
        existing: CheckpointRef | None,
    ) -> tuple[CheckpointRef, Path]:
        if existing is not None:
            self._recipe(plan, existing)
            return existing, self.store.checkpoint_path(existing)
        route = _route(boundary.publication.read.observation)
        proof = await checkpoints.capture(route, plan.staging / "capture")
        save = proof.file.path
        if (
            save.is_symlink()
            or save.resolve() != (plan.staging / "capture/current_run.save").resolve()
            or proof.state.route != route
        ):
            raise CheckpointUncertain("The capture proof does not describe the owned boundary.")
        checkpoint = CheckpointRef(
            sha256=proof.file.sha256,
            bytes=proof.file.bytes,
            boundary_sequence=plan.boundary_sequence,
            boundary_cursor=plan.boundary_cursor,
            target=proof.state.target,
        )
        self._recipe(plan, checkpoint)
        return checkpoint, save

    def _launch_recipe(self, plan: AdmissionPlan) -> StandardRunRecipe:
        conditions = plan.manifest.headless
        assert conditions is not None
        recipe = StandardRunRecipe(
            character=conditions.requested_character,
            ascension=conditions.requested_ascension,
            seed=plan.manifest.seed,
        )
        template = NativeTemplate(self.launch.template_manifest)
        if (
            template.evidence.manifest_sha256 != plan.manifest.template
            or template.evidence.settings_sha256 != conditions.effective_settings_sha256
            or template.evidence.game_assembly_sha256 != conditions.game_assembly_sha256
        ):
            raise AdmissionFailure(
                Unsupported(cause=UnsupportedCause.INITIAL_CONDITIONS, sequence=None)
            )
        return recipe

    def _recipe(self, plan: AdmissionPlan, checkpoint: CheckpointRef) -> None:
        conditions = plan.manifest.headless
        assert conditions is not None
        target = checkpoint.target
        if (
            target.canonical_seed != plan.manifest.seed
            or target.character != conditions.requested_character.value
            or target.ascension != conditions.requested_ascension
            or target.act != plan.boundary.state.progress.act
            or target.floor != plan.boundary.state.progress.floor
            or checkpoint.boundary_sequence != plan.boundary_sequence
            or checkpoint.boundary_cursor != plan.boundary_cursor
        ):
            raise AdmissionFailure(
                Unsupported(
                    cause=UnsupportedCause.INITIAL_CONDITIONS, sequence=plan.boundary_sequence
                )
            )

    @asynccontextmanager
    async def loaded(self, case: Case) -> AsyncGenerator[LoadedTarget]:
        binding = self.store.binding(case)
        reference = self.store.reference(binding)
        plan = plan_admission(
            AdmissionRequest(
                id=case.id,
                archive=case.source.archive,
                anchor=case.source.anchor,
                endpoint=case.endpoint,
            ),
            self.store.directory,
        )
        if plan.source != case.source:
            raise CheckpointUncertain("The original request dependency changed.")
        self._recipe(plan, case.checkpoint)
        path = self.store.checkpoint_path(case.checkpoint)
        recipe = self._launch_recipe(plan)
        async with OwnedHeadlessInstance(
            self.launch.options(recipe),
            mode=HeadlessLaunchMode.FRESH_MENU,
            development_checkpoints=True,
        ) as owner:
            url = f"http://127.0.0.1:{owner.port}"
            async with ModClient(url) as client, CheckpointClient(url) as checkpoints:
                proof = await checkpoints.load(path, case.checkpoint.sha256, case.checkpoint.target)
                if (
                    proof.state.target != case.checkpoint.target
                    or proof.file.sha256 != case.checkpoint.sha256
                    or proof.file.bytes != case.checkpoint.bytes
                ):
                    raise CheckpointUncertain(
                        "The load proof differs from the admitted checkpoint."
                    )
                read = await checkpoints.identity()
                if proof.state.route != _route(read.observation):
                    raise CheckpointUncertain("The loaded route differs from its native proof.")
                game = _live(client, read.observation)
                correspondence = LoadedCorrespondence()
                differences: list[AllowedMapDifference] = []
                difference = correspondence.compare(reference.boundary, read, allow_event_map=True)
                if difference is not None:
                    differences.append(difference)
                for write, keyed, expected in zip(
                    plan.suffix, binding.suffix, reference.suffix, strict=True
                ):
                    read = await checkpoints.identity()
                    difference = correspondence.compare(expected, read, allow_event_map=True)
                    if difference is not None:
                        differences.append(difference)
                    refs = loaded_refs(keyed, read)
                    await _write(game, client, read.observation, translated_step(write.step, refs))
                read = await checkpoints.identity()
                correspondence.compare(reference.target, read)
                loaded_refs(binding.target, read)
                yield LoadedTarget(
                    game, client, checkpoints, read.observation, tuple(differences), correspondence
                )

    async def verify_follow_on(
        self,
        loaded: LoadedTarget,
        plan: AdmissionPlan,
        reference: Reference,
        binding: ShortcutBinding,
    ) -> None:
        for write, keyed, before, expected in zip(
            plan.follow_on,
            binding.follow_on,
            reference.follow_on_before,
            reference.follow_on,
            strict=True,
        ):
            read = await loaded.checkpoints.identity()
            loaded.correspondence.compare(before, read)
            refs = loaded_refs(keyed, read)
            await _write(
                loaded.game, loaded.client, read.observation, translated_step(write.step, refs)
            )
            after = await loaded.checkpoints.identity()
            loaded.correspondence.compare(expected, after)
