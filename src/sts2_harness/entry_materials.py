"""Select configured entry guidance through the recorded public IO ports."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

from pydantic import MISSING

from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import DecisionInterrupted
from sts2_harness.game import GameLifetimeEnded, GameReadError
from sts2_harness.journal import SessionMaterials
from sts2_harness.knowledge import (
    Document,
    Documents,
    DocumentView,
    MaterialRole,
    SkillRead,
    SubjectId,
    UnknownDocument,
)
from sts2_harness.material_text import MaterialUnavailable
from sts2_harness.results import Reason, ReasonCode


@dataclass(frozen=True, slots=True)
class MaterialContext:
    ascension: int | None
    minimal: bool = False


@dataclass(frozen=True, slots=True)
class EntryMaterials:
    documents: Callable[[MaterialContext], Documents]
    settings: SessionMaterials


class _Stability(IntEnum):
    ACT = 0
    OWNED = 1
    CURRENT = 2


def _stable_documents(
    documents: tuple[Document, ...],
    act_subjects: tuple[SubjectId, ...],
    observation: native.Observation,
) -> tuple[Document, ...]:
    owned: list[SubjectId] = []
    state = observation.state
    if isinstance(state, native.RunState):
        for ref in (*state.deck, *state.relics, *state.potions):
            entry = observation.objects.get(ref)
            if isinstance(entry, native.CardEntry):
                category = native.NativeCategory.CARD
            elif isinstance(entry, native.RelicEntry):
                category = native.NativeCategory.RELIC
            elif isinstance(entry, native.PotionEntry):
                category = native.NativeCategory.POTION
            else:
                continue
            owned.append(SubjectId(category=category, definition_id=entry.definition_id))

    def stability(document: Document) -> _Stability:
        if any(subject in act_subjects for subject in document.subjects):
            return _Stability.ACT
        if any(subject in owned for subject in document.subjects):
            return _Stability.OWNED
        return _Stability.CURRENT

    return tuple(sorted(documents, key=stability))


async def entry_materials(
    execution: Execution,
    documents: Documents,
    role: MaterialRole,
    observation: native.Observation,
    configured: tuple[str, ...],
) -> tuple[DocumentView, ...]:
    """Select configured guidance and documents for current public identities."""
    selected: dict[str, DocumentView] = {}
    try:
        if role is not MaterialRole.DECK:
            for identity in configured:
                view = await documents.read(SkillRead(identity))
                selected[view.id] = view
        subjects: list[SubjectId] = []
        act_subjects: tuple[SubjectId, ...] = ()
        progress = observation.state.progress
        if role in (MaterialRole.RUN, MaterialRole.DECK):
            subjects.append(
                SubjectId(
                    category=native.NativeCategory.CHARACTER,
                    definition_id=observation.state.player.definition_id,
                )
            )
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
                if (
                    current.state.progress.act != progress.act
                    or current.location.owner is not observation.location.owner
                    or current.state.phase is not observation.state.phase
                ):
                    raise DecisionInterrupted(
                        Reason(
                            code=ReasonCode.ROLE_CHANGED, message="The current role or act changed."
                        ),
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
            act_subjects = tuple(subjects)
            if role is MaterialRole.RUN:
                subjects.extend(
                    SubjectId(
                        category=native.NativeCategory.CARD, definition_id=entry.definition_id
                    )
                    for entry in observation.objects.values()
                    if isinstance(entry, native.CardEntry)
                )
            elif isinstance(observation.state, native.RunState):
                state = observation.state
                refs = [*state.deck, *state.relics, *state.potions]
                refs.extend(ref for offer in state.offers for ref in offer.contents)
                if state.selection is not None:
                    refs.extend(state.selection.candidates)
                for ref in dict.fromkeys(refs):
                    entry = observation.objects.get(ref)
                    if entry is None:
                        raise MaterialUnavailable(
                            f"Current object {ref!r} is unavailable for guidance."
                        )
                    if isinstance(entry, native.CardEntry):
                        category = native.NativeCategory.CARD
                    elif isinstance(entry, native.RelicEntry):
                        category = native.NativeCategory.RELIC
                    elif isinstance(entry, native.PotionEntry):
                        category = native.NativeCategory.POTION
                    else:
                        continue
                    subjects.append(SubjectId(category=category, definition_id=entry.definition_id))
        elif isinstance(observation.state, native.CombatState):
            subjects.extend(combat_subjects(observation, native.DecisionOwner.COMBAT))
        unique = {(subject.category, subject.definition_id): subject for subject in subjects}
        matches = await documents.lookup(tuple(unique.values()))
        if role in (MaterialRole.RUN, MaterialRole.DECK):
            matches = _stable_documents(matches, act_subjects, observation)
        if role is MaterialRole.DECK:
            for identity in configured:
                match = next(
                    (doc for doc in matches if doc.id == identity and role in doc.auto_roles), None
                )
                if match is not None:
                    selected[match.id] = match.view()
        for document in matches:
            if role not in document.auto_roles:
                continue
            selected[document.id] = document.view()
    except UnknownDocument as error:
        raise MaterialUnavailable(str(error)) from error
    except (ClientUnavailable, GameReadError) as error:
        raise MaterialUnavailable(
            "Public identities required for guidance are unavailable."
        ) from error
    return tuple(selected.values())


def combat_subjects(
    observation: native.Observation, role: native.DecisionOwner
) -> tuple[SubjectId, ...]:
    """Select identities referenced by the current public battle state."""
    state = observation.state
    if not isinstance(state, native.CombatState):
        return ()
    subjects: list[SubjectId] = []
    encounter = observation.encounter
    if encounter is not MISSING and encounter.definition_id is not MISSING:
        subjects.append(
            SubjectId(
                category=native.NativeCategory.ENCOUNTER, definition_id=encounter.definition_id
            )
        )
    refs = [
        *state.piles.hand,
        *state.piles.draw,
        *state.piles.discard,
        *state.piles.exhaust,
        *state.piles.other,
        *state.relics,
        *state.potions,
    ]
    if observation.location.owner is role and state.selection is not None:
        refs.extend(state.selection.candidates)
    for ref in dict.fromkeys(refs):
        entry = observation.objects.get(ref)
        if entry is None:
            raise MaterialUnavailable(f"Current object {ref!r} is unavailable for guidance.")
        if isinstance(entry, native.CardEntry):
            category = native.NativeCategory.CARD
        elif isinstance(entry, native.RelicEntry):
            category = native.NativeCategory.RELIC
        elif isinstance(entry, native.PotionEntry):
            category = native.NativeCategory.POTION
        else:
            continue
        subjects.append(SubjectId(category=category, definition_id=entry.definition_id))
    powers = list(state.player.powers)
    for enemy in (*state.enemies, *state.inactive_enemies):
        subjects.append(
            SubjectId(category=native.NativeCategory.ENEMY, definition_id=enemy.definition_id)
        )
        powers.extend(enemy.powers)
    if state.allies is not MISSING:
        for ally in state.allies:
            powers.extend(ally.powers)
    subjects.extend(
        SubjectId(category=native.NativeCategory.POWER, definition_id=power.definition_id)
        for power in powers
    )
    if state.orbs is not MISSING:
        subjects.extend(
            SubjectId(category=native.NativeCategory.ORB, definition_id=orb.definition_id)
            for orb in state.orbs.slots
            if orb.definition_id is not MISSING
        )
    unique = {(subject.category, subject.definition_id): subject for subject in subjects}
    return tuple(unique.values())
