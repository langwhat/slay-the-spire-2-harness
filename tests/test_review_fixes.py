"""Behavioral and end-to-end tests for review fixes (auto, completeness, elites, notes)."""

import asyncio
import json
from io import StringIO
from pathlib import Path
from typing import cast

import httpx2 as httpx
from pydantic import JsonValue
from test_mod_client import COMBAT, RUN

from sts2_harness.auto import battle_reward, choose_auto
from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.client.http import CatalogResult, InspectResult
from sts2_harness.context import Context
from sts2_harness.context.map_projection import upcoming_act_elites
from sts2_harness.context.models import CombatActInput, RunActInput
from sts2_harness.decisions import ActionStep
from sts2_harness.deck import deck_observation
from sts2_harness.diagnostics import (
    Diagnostics,
    Event,
    EventKind,
    NoteBudgetExceeded,
    NoteBudgetField,
    measure_mechanisms,
)
from sts2_harness.entry_materials import entry_materials
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.journal import Manifest, SessionMaterials
from sts2_harness.knowledge import MaterialRole, SubjectId
from sts2_harness.llm.messages import ModelClient, TextMessage, ToolCall
from sts2_harness.materials import DocumentSource, FileDocuments
from sts2_harness.model_purpose import ModelPurpose
from sts2_harness.note_values import OTHER_SOFT_LIMIT, SUMMARY_SOFT_LIMIT, NotesState, NotesView
from sts2_harness.notes import Notes
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.results import NotesUpdated, ReadCompleted
from sts2_harness.route_plan import Edge, RoutePlan
from sts2_harness.run import RunRole
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness.tools import ToolCatalog, ToolName


def _rewards_observation(
    *,
    empty_potions: int,
    offers: tuple[native.Offer, ...],
    actions: tuple[native.Action, ...],
    relics: tuple[str, ...] = (),
    potions: tuple[str, ...] = (),
    extra_objects: dict[str, native.ObjectEntry] | None = None,
    room_type: str = "monster",
    screen: native.RunScreen = native.RunScreen.REWARDS,
    event: native.EventState | None = None,
) -> native.Observation:
    base = native.Observation.model_validate_json(RUN)
    state = base.state
    assert isinstance(state, native.RunState)
    return base.model_copy(
        update={
            "objects": {**base.objects, **(extra_objects or {})},
            "state": state.model_copy(
                update={
                    "screen": screen,
                    "control": native.StateControl.READY,
                    "selection": None,
                    "event": event,
                    "progress": state.progress.model_copy(update={"room_type": room_type}),
                    "player": state.player.model_copy(
                        update={"empty_potion_slot_count": empty_potions}
                    ),
                    "relics": relics,
                    "potions": potions,
                    "offers": offers,
                    "actions": actions,
                }
            ),
        }
    )


def _rendered_texts(context: Context) -> str:
    return "\n".join(
        message.text for message in context.model_input() if isinstance(message, TextMessage)
    )


def test_battle_reward_leaves_partial_potions_and_ambiguous_relics_to_run() -> None:
    two_potions = (
        native.Offer(
            ref="offer-p1",
            kind=native.OfferKind.POTION,
            contents=("pot-1",),
            actions=("claim-p1",),
        ),
        native.Offer(
            ref="offer-p2",
            kind=native.OfferKind.POTION,
            contents=("pot-2",),
            actions=("claim-p2",),
        ),
    )
    potion_actions = (
        native.Action(ref="claim-p1", label="Potion 1", enabled=True, subject="offer-p1"),
        native.Action(ref="claim-p2", label="Potion 2", enabled=True, subject="offer-p2"),
    )

    # Only 1 empty slot for 2 potions: Run must choose which potion to take.
    tight = _rewards_observation(empty_potions=1, offers=two_potions, actions=potion_actions)
    assert battle_reward(tight) is None

    # 2 empty slots for 2 potions: claiming is unambiguous.
    fits = _rewards_observation(empty_potions=2, offers=two_potions, actions=potion_actions)
    claimed = battle_reward(fits)
    assert claimed is not None and claimed.ref == "claim-p1"

    # Relic offer requiring selection or exposing multiple actions must not be auto-claimed.
    selective_relic = _rewards_observation(
        empty_potions=0,
        offers=(
            native.Offer(
                ref="offer-r",
                kind=native.OfferKind.RELIC,
                contents=("rel-1",),
                actions=("claim-r",),
                requires_selection=True,
            ),
        ),
        actions=(native.Action(ref="claim-r", label="Relic", enabled=True, subject="offer-r"),),
    )
    assert battle_reward(selective_relic) is None

    multi_action_relic = _rewards_observation(
        empty_potions=0,
        offers=(
            native.Offer(
                ref="offer-r",
                kind=native.OfferKind.RELIC,
                contents=("rel-1", "rel-2"),
                actions=("claim-r1", "claim-r2"),
            ),
        ),
        actions=(
            native.Action(ref="claim-r1", label="Relic 1", enabled=True, subject="offer-r"),
            native.Action(ref="claim-r2", label="Relic 2", enabled=True, subject="offer-r"),
        ),
    )
    assert battle_reward(multi_action_relic) is None


def test_shop_auto_skips_foul_potion_with_ectoplasm_or_fake_merchant() -> None:
    foul = native.PotionEntry(
        kind=native.ObjectKind.POTION,
        definition_id="POTION.FOUL_POTION",
        name="Foul Potion",
        usable=True,
        needs_target=False,
        target_type="none",
        usage="any_time",
    )
    ectoplasm = native.RelicEntry(
        kind=native.ObjectKind.RELIC,
        definition_id="RELIC.ECTOPLASM",
        name="Ectoplasm",
    )
    shop_actions = (
        native.Action(
            ref="open",
            label="Open Shop",
            enabled=True,
            operation=native.ActionOperation.OPEN_SHOP,
        ),
        native.Action(
            ref="leave",
            label="Leave",
            enabled=True,
            operation=native.ActionOperation.LEAVE_SHOP,
        ),
    )

    with_ectoplasm = _rewards_observation(
        empty_potions=1,
        offers=(),
        actions=shop_actions,
        relics=("rel-ecto",),
        potions=("pot-foul",),
        extra_objects={"rel-ecto": ectoplasm, "pot-foul": foul},
        room_type="shop",
        screen=native.RunScreen.SHOP,
    )
    choice = choose_auto(with_ectoplasm, shop_strategy=False)
    assert choice is not None
    step = choice[0].steps[0]
    assert isinstance(step, ActionStep)
    assert step.ref == "open"

    fake_merchant = _rewards_observation(
        empty_potions=1,
        offers=(),
        actions=shop_actions,
        potions=("pot-foul",),
        extra_objects={"pot-foul": foul},
        room_type="event",
        screen=native.RunScreen.EVENT,
        event=native.EventState(
            ref="ev-fake",
            name="Fake Merchant",
            ancient=False,
            definition_id="EVENT.FAKE_MERCHANT",
        ),
    )
    choice_fake = choose_auto(fake_merchant, shop_strategy=False)
    assert choice_fake is not None
    step_fake = choice_fake[0].steps[0]
    assert isinstance(step_fake, ActionStep)
    assert step_fake.ref == "open"


def test_partial_and_unavailable_enemy_mechanisms_reach_combat_context() -> None:
    base = native.Observation.model_validate_json(COMBAT)
    partial_rule = native.MechanismView(
        kind=native.MechanismKind.OTHER,
        definition_id="MONSTER.PARTIAL",
        name="Partial Monster",
        text="First move: KNOWN_MOVE\nKNOWN_MOVE: gain 5 Block → BROKEN_MOVE",
        owner="enemy-1",
        completeness=native.MechanismCompleteness.PARTIAL,
    )
    unavailable_rule = native.MechanismView(
        kind=native.MechanismKind.OTHER,
        definition_id="MONSTER.BROKEN",
        name="Broken Monster",
        text=None,
        owner="enemy-2",
        completeness=native.MechanismCompleteness.UNAVAILABLE,
    )
    observation = base.model_copy(
        update={"mechanisms": (*base.mechanisms, partial_rule, unavailable_rule)}
    )
    context = Context(native.DecisionOwner.COMBAT, system=())
    context.begin_group(observation, NotesView(), None)
    context.append_task(CombatActInput(), observation)
    rendered = _rendered_texts(context)
    assert '"completeness":"partial"' in rendered
    assert '"completeness":"unavailable"' in rendered
    assert "MONSTER.PARTIAL" in rendered
    assert "MONSTER.BROKEN" in rendered

    # Compaction preserves both partial and unavailable (text=None) mechanisms cleanly.
    candidate, _ = context.compaction_input()
    context.validate_compaction(candidate)
    context.install_compaction(candidate)
    compacted_text = _rendered_texts(context)
    assert '"completeness":"partial"' in compacted_text
    assert '"completeness":"unavailable"' in compacted_text


def test_deck_receives_reachable_elite_mechanisms_and_guidance_only_when_elites_ahead(
    tmp_path: Path,
) -> None:
    base = native.Observation.model_validate_json(RUN)
    state = base.state
    assert isinstance(state, native.RunState)
    public_map = native.PublicMap(
        bosses=(native.NamedDefinition(name="Act Boss", definition_id="ENCOUNTER.BOSS"),),
        elite_pool=(native.NamedDefinition(name="Elite One", definition_id="ENCOUNTER.ELITE_ONE"),),
        nodes=(
            native.MapNode(
                position="map:0:0", room_type="monster", children=("map:1:0", "map:1:1")
            ),
            native.MapNode(position="map:1:0", room_type="elite", children=("map:2:0",)),
            native.MapNode(position="map:1:1", room_type="monster", children=("map:2:0",)),
            native.MapNode(position="map:2:0", room_type="boss", children=()),
        ),
        legal_destinations=("map:1:0", "map:1:1"),
    )
    elite_mech = native.MechanismView(
        kind=native.MechanismKind.OTHER,
        definition_id="ENCOUNTER.ELITE_ONE",
        name="Elite One",
        text="Monsters: Elite One",
        owner="act",
    )
    elite_power = native.MechanismView(
        kind=native.MechanismKind.POWER,
        definition_id="POWER.ELITE_THORNS",
        name="Thorns",
        text="Deals 3 damage back.",
        owner="act:ENCOUNTER.ELITE_ONE",
    )
    boss_mech = native.MechanismView(
        kind=native.MechanismKind.OTHER,
        definition_id="ENCOUNTER.BOSS",
        name="Act Boss",
        text="Monsters: Act Boss",
        owner="act",
    )
    observation = base.model_copy(
        update={
            "map": public_map,
            "state": state.model_copy(
                update={
                    "progress": state.progress.model_copy(
                        update={"position": "map:0:0", "room_type": "monster", "bosses_left": 1}
                    )
                }
            ),
            "mechanisms": (boss_mech, elite_mech, elite_power),
        }
    )

    # Without a route plan, Elite at map:1:0 is reachable -> Deck sees Elite rules & power.
    with_elite = deck_observation(observation, ())
    mech_ids = {m.definition_id for m in with_elite.mechanisms}
    assert {"ENCOUNTER.BOSS", "ENCOUNTER.ELITE_ONE", "POWER.ELITE_THORNS"} <= mech_ids

    # Focused Deck context projects progress.elite_pool when reachable.
    ctx = Context(native.DecisionOwner.RUN, system=(), focused_deck=True)
    ctx.append_task(RunActInput(), with_elite)
    ctx_text = _rendered_texts(ctx)
    assert "ENCOUNTER.ELITE_ONE" in ctx_text
    assert '"elite_pool"' in ctx_text

    # With a complete route plan avoiding Elites (map:0:0 -> map:1:1 -> map:2:0), omit Elites.
    avoid_elites = RoutePlan(
        position="map:0:0",
        reason="avoid elite",
        edges=(Edge("map:0:0", "map:1:1"), Edge("map:1:1", "map:2:0")),
    )
    without_elite = deck_observation(observation, (), route_plan=avoid_elites)
    assert {m.definition_id for m in without_elite.mechanisms} == {"ENCOUNTER.BOSS"}

    # Partial route stopping before Boss with an Elite reachable after its tail still keeps Elites.
    branched_map = native.PublicMap(
        bosses=(native.NamedDefinition(name="Act Boss", definition_id="ENCOUNTER.BOSS"),),
        elite_pool=(native.NamedDefinition(name="Elite One", definition_id="ENCOUNTER.ELITE_ONE"),),
        nodes=(
            native.MapNode(position="map:0:0", room_type="monster", children=("map:1:0",)),
            native.MapNode(position="map:1:0", room_type="monster", children=("map:2:0",)),
            native.MapNode(position="map:2:0", room_type="elite", children=("map:3:0",)),
            native.MapNode(position="map:3:0", room_type="boss", children=()),
        ),
        legal_destinations=("map:1:0",),
    )
    partial_plan = RoutePlan(
        position="map:0:0", reason="partial", edges=(Edge("map:0:0", "map:1:0"),)
    )
    assert len(upcoming_act_elites(branched_map, observation.state.progress, partial_plan)) == 1
    stale_plan = RoutePlan(
        position="map:0:0", reason="off-map", edges=(Edge("map:0:0", "map:9:9"),)
    )
    assert len(upcoming_act_elites(branched_map, observation.state.progress, stale_plan)) == 1
    no_pos = observation.state.progress.model_copy(update={"position": None})
    assert len(upcoming_act_elites(branched_map, no_pos, None)) == 1

    # Entry materials for Deck match Elite encounter docs when reachable and omit when avoided.
    elite_doc = tmp_path / "elite.md"
    elite_doc.write_text("Elite One guidance.", encoding="utf-8")
    docs = FileDocuments(
        (
            DocumentSource(
                "elite-one",
                "Elite One",
                "Elite rules",
                elite_doc,
                subjects=(
                    SubjectId(
                        category=native.NativeCategory.ENCOUNTER,
                        definition_id="ENCOUNTER.ELITE_ONE",
                    ),
                ),
                auto_roles=(MaterialRole.RUN, MaterialRole.DECK),
            ),
        )
    )

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected mod call")

    class _StubRun:
        def __init__(self, plan: RoutePlan | None) -> None:
            self.route_plan = plan

    async def check_materials() -> None:
        async with ModClient(
            "http://mod", transport=httpx.MockTransport(unexpected_request)
        ) as client:
            live = LiveGame(client, namespace="test", game_instance_id="game", run_id="run")
            execution = Execution(live, game_instance_id="game", run_id="run")
            found_with = await entry_materials(execution, docs, MaterialRole.DECK, observation, ())
            assert tuple(d.id for d in found_with) == ("elite-one",)
            found_without = await entry_materials(
                execution, docs, MaterialRole.DECK, observation, (), route_plan=avoid_elites
            )
            assert found_without == ()

            # RunSession._materials wires the active RunRole's reconciled route_plan into Deck/Run.
            stub_run = _StubRun(avoid_elites)
            manifest = Manifest(
                recording_id="rec-1",
                core_version="0.1.0",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                materials=SessionMaterials(
                    act_guidance={},
                    battle_guidance=(),
                    entry_documents_enabled=True,
                ),
            )

            def unused_model(_purpose: ModelPurpose, _stream: str) -> ModelClient:
                raise AssertionError("unexpected model call")

            def stub_factory(_exec: Execution, _notes: Notes, _rec: ToolRecoveryBudget) -> RunRole:
                return cast(RunRole, stub_run)

            deps = SessionDependencies(
                game=live,
                model=unused_model,
                prompts=PackagedPrompts(),
                documents=docs,
            )
            session = RunSession(
                manifest,
                deps,
                run_factory=stub_factory,
            )
            async with session:
                reader = session._materials(MaterialRole.DECK)  # pyright: ignore[reportPrivateUsage]
                assert reader is not None
                assert await reader(observation) == ()
                stub_run.route_plan = None
                assert tuple(d.id for d in await reader(observation)) == ("elite-one",)

    asyncio.run(check_materials())


def test_measure_mechanisms_tracks_tokens_and_cross_node_repeats() -> None:
    seen: set[tuple[str, str, str | None, str | None]] = set()
    batch1: list[dict[str, JsonValue]] = [
        {
            "kind": "other",
            "definition_id": "ENCOUNTER.BOSS",
            "name": "Boss",
            "text": "First move: SLAM",
        },
        {
            "kind": "power",
            "definition_id": "POWER.STRENGTH",
            "name": "Strength",
            "text": "Increases attack damage.",
        },
    ]
    first = measure_mechanisms(batch1, seen_keys=seen, role=native.DecisionOwner.RUN)
    assert first.event is EventKind.MECHANISM_METRICS
    assert first.mechanism_count == 2
    assert first.mechanism_characters > 0
    assert first.mechanism_tokens > 0
    assert first.repeated_mechanism_count == 0
    assert first.repeated_mechanism_tokens == 0

    second = measure_mechanisms(batch1, seen_keys=seen, role=native.DecisionOwner.RUN)
    assert second.repeated_mechanism_count == 2
    assert second.repeated_mechanism_tokens == second.mechanism_tokens


def test_soft_note_budget_accepts_over_limit_writes_without_rejecting() -> None:
    base = native.Observation.model_validate_json(RUN)
    notes = Notes(NotesState())
    buf = StringIO()
    recorded_fields: list[NoteBudgetField] = []

    class RecordingDiagnostics(Diagnostics):
        def emit(self, event: Event) -> None:
            if isinstance(event, NoteBudgetExceeded):
                recorded_fields.append(event.field)
            super().emit(event)

    diagnostics = RecordingDiagnostics("soft-notes", console=buf)

    async def observe() -> native.Observation:
        return base

    async def unexpected_inspect(_query: native.InspectQuery) -> InspectResult:
        raise AssertionError("unexpected inspect")

    async def unexpected_catalog(_query: native.CatalogRequest) -> CatalogResult:
        raise AssertionError("unexpected catalog")

    catalog = ToolCatalog(
        native.DecisionOwner.RUN,
        observe=observe,
        inspect=unexpected_inspect,
        catalog=unexpected_catalog,
        notes=notes,
        diagnostics=diagnostics,
    )
    long_other = "x" * (OTHER_SOFT_LIMIT + 50)
    long_summary = "y" * (SUMMARY_SOFT_LIMIT + 50)
    call = ToolCall(
        id="call-1",
        name=ToolName.RUN_NOTES,
        arguments=json.dumps({"mode": "write", "other": long_other, "summary": long_summary}),
    )

    result = asyncio.run(catalog.dispatch(call, handoff=True))
    assert isinstance(result.outcome, ReadCompleted)
    assert isinstance(result.data, NotesUpdated)
    assert result.data.summary == long_summary
    assert notes.read().other == long_other
    assert recorded_fields == [NoteBudgetField.OTHER, NoteBudgetField.SUMMARY]
