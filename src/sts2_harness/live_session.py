"""Own live clients and recording decorators outside the run's software state."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path

from sts2_harness.client import ModClient
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.game import LiveGame
from sts2_harness.journal import ArchiveError, Journal, Manifest, RecordedGame
from sts2_harness.knowledge import CandidateLinks, Documents
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.materials import FileDocuments
from sts2_harness.model_journal import (
    RecordedCandidateLinks,
    RecordedDocuments,
    RecordedLlm,
    RecordedPrompts,
)
from sts2_harness.model_purpose import ModelPurpose
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.session import (
    FlowFactory,
    RoleFactory,
    RunFactory,
    RunSession,
    SessionDependencies,
)
from sts2_harness.user_input_values import UserInputSource


@asynccontextmanager
async def live_session(
    client: ModClient,
    manifest: Manifest,
    archive_path: Path,
    *,
    model: Llm | None = None,
    documents: Documents | None = None,
    candidate_links: CandidateLinks | None = None,
    run_factory: RunFactory | None = None,
    combat_factory: RoleFactory | None = None,
    flow_factory: FlowFactory | None = None,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    diagnostics: Diagnostics | None = None,
    stop_requested: Callable[[], bool] | None = None,
    user_input: UserInputSource | None = None,
) -> AsyncGenerator[RunSession]:
    if model is None and (run_factory is None or combat_factory is None):
        raise ValueError("Default model roles require a model resource.")
    if documents is None and (
        manifest.materials.entry_documents_enabled
        or manifest.materials.act_guidance
        or manifest.materials.battle_guidance
    ):
        raise ValueError("Configured guidance requires a document provider.")
    async with AsyncExitStack() as resources:
        await resources.enter_async_context(client)
        models: dict[ModelPurpose, Llm] = {}
        if model is not None:
            await resources.enter_async_context(model)
            for purpose in ModelPurpose:
                models[purpose] = model.for_purpose(purpose, recording_id=manifest.recording_id)
        journal = Journal.create(archive_path, manifest, diagnostics=diagnostics)

        def close_recording() -> None:
            # Journal reports sealing failures; ordinary gameplay and cancellation still finish.
            with suppress(ArchiveError):
                journal.close()

        resources.callback(close_recording)
        game = RecordedGame(
            LiveGame(
                client,
                namespace=manifest.recording_id,
                game_instance_id=manifest.game_instance_id,
                run_id=manifest.run_id,
                wait_for_start=True,
                audit=journal,
                wait=wait,
                stop_requested=stop_requested,
                on_status=diagnostics.native_status if diagnostics is not None else None,
            ),
            journal,
        )

        def model_client(purpose: ModelPurpose, stream: str) -> ModelClient:
            if model is None:
                raise ValueError("Default model roles require a model resource.")
            return RecordedLlm(models[purpose], journal, role=purpose.owner, stream=stream)

        dependencies = SessionDependencies(
            game,
            RecordedPrompts(PackagedPrompts(), journal),
            RecordedDocuments(documents if documents is not None else FileDocuments(), journal),
            model_client,
            RecordedCandidateLinks(candidate_links, journal)
            if candidate_links is not None
            else None,
            journal.record_card_choice,
        )
        async with RunSession(
            manifest,
            dependencies,
            run_factory=run_factory,
            combat_factory=combat_factory,
            flow_factory=flow_factory,
            diagnostics=journal.diagnostics,
            user_input=user_input,
        ) as session:
            yield session
