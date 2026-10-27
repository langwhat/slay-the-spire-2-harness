"""Select configured entry guidance through the recorded public IO ports."""

from pydantic import MISSING

from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import DecisionInterrupted
from sts2_harness.game import GameLifetimeEnded, GameReadError
from sts2_harness.knowledge import DocumentView, SkillRead, SubjectId, UnknownDocument
from sts2_harness.material_text import MaterialUnavailable
from sts2_harness.model_journal import RecordedDocuments
from sts2_harness.results import Reason, ReasonCode


async def entry_materials(
    execution: Execution,
    documents: RecordedDocuments,
    role: native.DecisionOwner,
    observation: native.Observation,
    configured: tuple[str, ...],
) -> tuple[DocumentView, ...]:
    """Select configured guidance using public act, Boss, and enemy identities."""
    if not await documents.list(role) and not configured:
        return ()
    selected: dict[str, DocumentView] = {}
    try:
        for identity in configured:
            view = await documents.read(SkillRead(identity))
            selected[view.id] = view
        subjects: list[SubjectId] = []
        progress = observation.state.progress
        if role is native.DecisionOwner.RUN:
            if progress.definition_id is not MISSING:
                subjects.append(SubjectId(category="act", definition_id=progress.definition_id))
            map_ = observation.map
            if map_ is MISSING:
                result = await execution.inspect(native.MapQuery(area=native.InspectArea.MAP))
                if not isinstance(result, native.InspectCompleted):
                    raise MaterialUnavailable("The act map required for guidance is unavailable.")
                current = result.observation
                if (
                    current.location.game_instance_id != observation.location.game_instance_id
                    or current.location.run_id != observation.location.run_id
                    or current.state.phase is native.Phase.TERMINAL
                ):
                    raise GameLifetimeEnded(current.location)
                if current.state.progress.act != progress.act:
                    raise DecisionInterrupted(
                        Reason(code=ReasonCode.ROLE_CHANGED, message="The current act changed."),
                        False,
                    )
                if not isinstance(result.data, native.MapData):
                    raise MaterialUnavailable("The act map required for guidance is unavailable.")
                map_ = result.data.map
            subjects.extend(
                SubjectId(
                    category=native.NativeCategory.ENCOUNTER, definition_id=boss.definition_id
                )
                for boss in map_.bosses
                if isinstance(boss.definition_id, str)
            )
        elif isinstance(observation.state, native.CombatState):
            encounter = observation.encounter
            if encounter is not MISSING and encounter.definition_id is not MISSING:
                subjects.append(
                    SubjectId(
                        category=native.NativeCategory.ENCOUNTER,
                        definition_id=encounter.definition_id,
                    )
                )
            subjects.extend(
                SubjectId(category=native.NativeCategory.ENEMY, definition_id=enemy.definition_id)
                for enemy in (*observation.state.enemies, *observation.state.inactive_enemies)
            )
        unique = {(subject.category, subject.definition_id): subject for subject in subjects}
        matches = await documents.lookup(tuple(unique.values()))
        for document in matches:
            selected[document.id] = document.view()
    except UnknownDocument as error:
        raise MaterialUnavailable(str(error)) from error
    except (ClientUnavailable, GameReadError) as error:
        raise MaterialUnavailable(
            "Public identities required for guidance are unavailable."
        ) from error
    return tuple(selected.values())
