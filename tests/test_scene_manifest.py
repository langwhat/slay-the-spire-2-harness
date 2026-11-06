from contextlib import closing
from pathlib import Path

import pytest
from test_mod_client import COMBAT, RUN

from sts2_harness.client import models as native
from sts2_harness.journal import (
    Journal,
    Manifest,
    SceneInitialConditions,
    SceneRestore,
    SceneSource,
    SceneVariant,
    SessionMaterials,
)


@pytest.mark.parametrize("observation_json", [RUN, COMBAT], ids=["run", "combat"])
def test_restored_scene_records_a_run_or_combat_start(
    tmp_path: Path, observation_json: str
) -> None:
    observation = native.Observation.model_validate_json(observation_json)
    progress = observation.state.progress
    assert progress.act is not None and progress.floor is not None
    assert observation.location.run_id is not None
    initial = SceneInitialConditions(
        observation=observation,
        control=native.ControlReply(mode=native.ControlIntent.RUNNING),
        restore=SceneRestore(
            checkpoint_sha256="a" * 64,
            checkpoint_bytes=1,
            canonical_seed="SEED",
            game_mode="standard",
            character=observation.state.player.definition_id,
            ascension=10,
            act=progress.act,
            floor=progress.floor,
            route=native.RouteKey.model_validate(observation.location.model_dump()),
            native_run_instance_id=1,
            native_revision=1,
        ),
        template="template",
        game_version="version",
        effective_settings_sha256="settings",
        game_assembly_sha256="assembly",
        mod_version="mod",
        mod_sha256="mod-sha",
    )
    manifest = Manifest(
        recording_id="restored",
        core_version="test",
        game_instance_id=observation.location.game_instance_id,
        run_id=observation.location.run_id,
        materials=SessionMaterials(
            entry_documents_enabled=False, act_guidance={}, battle_guidance=()
        ),
        initial_notes="Existing build plan.",
        seed="SEED",
        template="template",
        game_version="version",
        variant=SceneVariant(
            case_id="rest-site",
            case_sha256="b" * 64,
            source=SceneSource(
                archive=tmp_path / "source.sqlite",
                recording_id="source",
                model_sequence=1,
                archive_sha256="c" * 64,
            ),
            replacements=(),
            initial=initial,
        ),
    )
    archive = tmp_path / "scene.sqlite"
    Journal.create(archive, manifest).close()
    with closing(Journal.open(archive)) as reopened:
        assert reopened.manifest == manifest
        assert reopened.manifest.initial_notes == "Existing build plan."
