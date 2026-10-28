"""Own live clients and recording decorators outside the run's software state."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.game import LiveGame
from sts2_harness.journal import Journal, Manifest, RecordedGame
from sts2_harness.knowledge import Documents
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.materials import FileDocuments
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.session import (
    FlowFactory,
    RoleFactory,
    RunFactory,
    RunSession,
    SessionDependencies,
)


@asynccontextmanager
async def live_session(
    client: ModClient,
    manifest: Manifest,
    archive_path: Path,
    *,
    model: Llm | None = None,
    documents: Documents | None = None,
    act_guidance: Mapping[int, tuple[str, ...]] | None = None,
    battle_guidance: tuple[str, ...] = (),
    run_factory: RunFactory | None = None,
    combat_factory: RoleFactory | None = None,
    flow_factory: FlowFactory | None = None,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    diagnostics: Diagnostics | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> AsyncGenerator[RunSession]:
    if model is None and (run_factory is None or combat_factory is None):
        raise ValueError("Default model roles require a model resource.")
    if documents is None and (act_guidance or battle_guidance):
        raise ValueError("Configured guidance requires a document provider.")
    async with AsyncExitStack() as resources:
        await resources.enter_async_context(client)
        if model is not None:
            await resources.enter_async_context(model)
        journal = Journal.create(archive_path, manifest, diagnostics=diagnostics)
        resources.callback(journal.close)
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

        def model_client(role: native.DecisionOwner, stream: str) -> ModelClient:
            if model is None:
                raise ValueError("Default model roles require a model resource.")
            return RecordedLlm(model, journal, role=role, stream=stream)

        dependencies = SessionDependencies(
            game,
            RecordedPrompts(PackagedPrompts(), journal),
            RecordedDocuments(documents if documents is not None else FileDocuments(), journal),
            model_client,
        )
        async with RunSession(
            manifest,
            dependencies,
            entry_documents_enabled=documents is not None,
            act_guidance=act_guidance,
            battle_guidance=battle_guidance,
            run_factory=run_factory,
            combat_factory=combat_factory,
            flow_factory=flow_factory,
            diagnostics=journal.diagnostics,
        ) as session:
            yield session
