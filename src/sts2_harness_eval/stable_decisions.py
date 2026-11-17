"""Evaluate a fresh decision against an immutable observation without game writes."""

import json
from enum import StrEnum
from time import monotonic

from pydantic import MISSING, BaseModel, JsonValue, TypeAdapter, ValidationError

from sts2_harness.agent import Agent
from sts2_harness.client import models as native
from sts2_harness.client.http import InspectResult
from sts2_harness.combat import CombatAgent
from sts2_harness.context import Context, same_observation
from sts2_harness.context.map_projection import MapRoom, MapUnavailable, replace_stretches
from sts2_harness.context.models import TaskMessage
from sts2_harness.decisions import ActionProposal, ActTask, DecisionTask, LocalProgress
from sts2_harness.deck_role import DeckAgent
from sts2_harness.entry_materials import entry_materials
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.fights import FightHistory
from sts2_harness.game import Cataloged, GameReply, Inspect, Inspected
from sts2_harness.knowledge import DocumentView, MaterialRole
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    CapacityRejected,
    FailureKind,
    ModelInput,
    ModelReply,
    ModelUnavailable,
    PreparedRequest,
    RequestPreparation,
    TextMessage,
    ToolCall,
    ToolDefinition,
)
from sts2_harness.note_values import NotesView
from sts2_harness.notes import Notes
from sts2_harness.prompts import PackagedPrompts, PromptReader
from sts2_harness.results import Failed, QueryToolResult, ReadCompleted, ReasonCode, Rejected
from sts2_harness.route_plan import PlanRejection, RoutePlan
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog, ToolName, unique_fields
from sts2_harness.tools.inputs import CombatActInput, RunActInput, RunNext, RunPlan
from sts2_harness.user_input import UserInputQueue
from sts2_harness_eval.decision_cases import (
    CapturedProposal,
    DecisionCase,
    DecisionSample,
    DecisionVerdict,
    ProposalStatus,
    RouteMetrics,
    TaskEvent,
    score,
)
from sts2_harness_eval.decision_extract import ExtractionError, choices_for, decision_input
from sts2_harness_eval.decision_replay import CaseGame, CaseStatistics, MeasuredModel
from sts2_harness_eval.frozen_catalog import FrozenCatalog, SnapshotIncomplete
from sts2_harness_eval.frozen_documents import FrozenDocuments, FrozenLinks

_OBJECT = TypeAdapter(dict[str, JsonValue])


class EvaluationRole(StrEnum):
    RUN = "run"
    COMBAT = "combat"
    DECK = "deck"


class NoProposal(RuntimeError):
    """The trial ended without a model action proposal."""


class Captured(Exception):
    def __init__(self, call: ToolCall, status: ProposalStatus, error: str | None = None) -> None:
        self.call = call
        self.status = status
        self.error = error
        super().__init__(error or "First proposal captured before execution.")


class CaptureDecision(Agent):
    decision_names: frozenset[ToolName]
    all_decisions = frozenset(
        (
            ToolName.RUN_ACT,
            ToolName.RUN_NEXT,
            ToolName.RUN_PLAN,
            ToolName.RUN_DECK_CHOICE,
            ToolName.COMBAT_ACT,
        )
    )
    _tools: ToolCatalog
    route_metrics: RouteMetrics | None = None

    def focus_observation(self, observation: native.Observation) -> native.Observation:
        return observation

    async def prepare_fresh(self, observation: native.Observation) -> None:
        context = await self._prepare(observation)
        context.set_notes(NotesView())
        context.set_route_plan(None)
        context.begin_group(observation, NotesView())

    def _close_query(
        self,
        context: Context,
        task: DecisionTask,
        bound: native.Location,
        call: ToolCall,
        result: QueryToolResult,
    ) -> native.Observation | None:
        observation = super()._close_query(context, task, bound, call, result)
        if not isinstance(result.outcome, ReadCompleted):
            raise NoProposal("The query was rejected or failed; this trial is terminal unknown.")
        return observation

    def _reject_action_layout(
        self, context: Context, calls: tuple[ToolCall, ...], observation: native.Observation
    ) -> None:
        first = next(call for call in calls if call.name in self.all_decisions)
        raise Captured(first, ProposalStatus.UNKNOWN, "Invalid action layout.")

    async def _validate_proposal(
        self,
        call: ToolCall,
        task: DecisionTask,
        observation: native.Observation,
        parameters: BaseModel | None,
    ) -> None:
        return None

    async def _dispatch(
        self, call: ToolCall, task: DecisionTask, observation: native.Observation
    ) -> ActionProposal | QueryToolResult | LocalProgress:
        if call.name in self.all_decisions:
            definition = next(
                (item for item in self._tools.definitions if item.name == call.name), None
            )
            if definition is None or call.name not in self.decision_names:
                raise Captured(
                    call, ProposalStatus.INVALID, "The action is unavailable to this role."
                )
            if call.name == ToolName.RUN_DECK_CHOICE:
                await self._validate_proposal(call, task, observation, None)
                raise Captured(call, ProposalStatus.VALID)
            try:
                json.loads(call.arguments, object_pairs_hook=unique_fields)
                parameters = definition.parameters.model_validate_json(call.arguments, strict=True)
            except (ValueError, ValidationError) as error:
                raise Captured(call, ProposalStatus.INVALID, str(error)) from error
            await self._validate_proposal(call, task, observation, parameters)
            raise Captured(call, ProposalStatus.VALID)
        return await super()._dispatch(call, task, observation)


def _forced_elite_before_recovery(
    position: str, nodes: dict[str, native.MapNode], visited: frozenset[str] = frozenset()
) -> bool | None:
    node = nodes.get(position)
    if node is None or position in visited:
        return None
    if node.room_type == MapRoom.ELITE:
        return True
    if node.room_type in (MapRoom.REST, MapRoom.BOSS) or not node.children:
        return False
    outcomes = tuple(
        _forced_elite_before_recovery(child, nodes, visited | {position}) for child in node.children
    )
    if False in outcomes:
        return False
    return None if None in outcomes else True


def _route_metrics(observation: native.Observation, plan: RoutePlan) -> RouteMetrics:
    assert isinstance(observation.state, native.RunState) and observation.map is not MISSING
    nodes = {node.position: node for node in observation.map.nodes}
    rooms = tuple(nodes[edge.destination].room_type for edge in plan.edges)
    player = observation.state.player
    low_hp = player.hp * 5 <= player.max_hp * 2 if player.max_hp else None
    forced = _forced_elite_before_recovery(plan.edges[0].destination, nodes)
    bad = None if low_hp is None or (low_hp and forced is None) else low_hp and forced is True
    return RouteMetrics(
        full_route=rooms[-1] == MapRoom.BOSS and not nodes[plan.edges[-1].destination].children,
        planned_elites=rooms.count(MapRoom.ELITE),
        hp=player.hp,
        max_hp=player.max_hp,
        low_hp=low_hp,
        forced_elite_before_recovery=forced,
        bad_low_hp_route=bad,
    )


class RunProbe(CaptureDecision, RunAgent):
    decision_names = frozenset((ToolName.RUN_ACT, ToolName.RUN_NEXT, ToolName.RUN_PLAN))

    async def _validate_proposal(
        self,
        call: ToolCall,
        task: DecisionTask,
        observation: native.Observation,
        parameters: BaseModel | None,
    ) -> None:
        if isinstance(parameters, RunActInput):
            try:
                _verify_run_action(parameters, observation)
            except ExtractionError as error:
                raise Captured(call, ProposalStatus.INVALID, str(error)) from error
            except SnapshotIncomplete as error:
                raise Captured(call, ProposalStatus.UNKNOWN, str(error)) from error
            return
        if isinstance(parameters, RunNext):
            if self._plan is None:
                raise Captured(
                    call,
                    ProposalStatus.INVALID,
                    f"No route plan is active ({ReasonCode.NO_PLAN.value}).",
                )
            raise Captured(call, ProposalStatus.UNKNOWN, "Route travel cannot be verified offline.")
        if isinstance(parameters, RunPlan):
            if not isinstance(observation.state, native.RunState) or observation.map is MISSING:
                raise Captured(call, ProposalStatus.UNKNOWN, "Current map facts are unavailable.")
            try:
                update = replace_stretches(
                    self._plan,
                    observation.map,
                    observation.state.progress.position,
                    parameters.reason,
                    parameters.stretches,
                )
            except MapUnavailable as error:
                raise Captured(call, ProposalStatus.UNKNOWN, str(error)) from error
            if update.rejection is not None:
                raise Captured(
                    call,
                    ProposalStatus.UNKNOWN
                    if update.rejection
                    in (PlanRejection.DESTINATIONS_UNKNOWN, PlanRejection.UNKNOWN_POSITION)
                    else ProposalStatus.INVALID,
                    f"The route plan was rejected: {update.rejection.value}.",
                )
            assert update.plan is not None
            self.route_metrics = _route_metrics(observation, update.plan)
            return

    def focus_observation(self, observation: native.Observation) -> native.Observation:
        return self._reward_view(observation)


class CombatProbe(CaptureDecision, CombatAgent):
    decision_names = frozenset((ToolName.COMBAT_ACT,))

    async def _validate_proposal(
        self,
        call: ToolCall,
        task: DecisionTask,
        observation: native.Observation,
        parameters: BaseModel | None,
    ) -> None:
        if isinstance(parameters, CombatActInput):
            if (
                observation.state.control is not native.StateControl.READY
                or observation.input_readiness
                not in (native.InputReadiness.READY, native.InputReadiness.SELECTION)
            ):
                raise Captured(
                    call, ProposalStatus.INVALID, "The combat state is not ready for an action."
                )
            try:
                _verify_initial_selection(parameters.root.steps[0], observation)
            except ExtractionError as error:
                raise Captured(call, ProposalStatus.INVALID, str(error)) from error
            except SnapshotIncomplete as error:
                raise Captured(call, ProposalStatus.UNKNOWN, str(error)) from error
            raise Captured(
                call, ProposalStatus.UNKNOWN, "Combat action legality cannot be verified offline."
            )


class DeckProbe(CaptureDecision, DeckAgent):
    decision_names = frozenset((ToolName.RUN_DECK_CHOICE,))

    async def _validate_proposal(
        self,
        call: ToolCall,
        task: DecisionTask,
        observation: native.Observation,
        parameters: BaseModel | None,
    ) -> None:
        if (
            observation.state.control is not native.StateControl.READY
            or observation.input_readiness
            not in (native.InputReadiness.READY, native.InputReadiness.SELECTION)
        ):
            raise Captured(call, ProposalStatus.INVALID, "The card choice is not ready.")
        result = await self._deck_choice(call, task)
        if isinstance(result, QueryToolResult) and isinstance(result.outcome, (Rejected, Failed)):
            reason = result.outcome.reason
            status = (
                ProposalStatus.UNKNOWN
                if reason.code is ReasonCode.QUERY_FAILED
                else ProposalStatus.INVALID
            )
            raise Captured(call, status, reason.message)

    def focus_observation(self, observation: native.Observation) -> native.Observation:
        return self._reward_view(observation)


class FreshModel(MeasuredModel):
    history: native.HistoryData | None = None
    last_calls: tuple[ToolCall, ...] = ()

    def prepare(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> PreparedRequest:
        if self.history is None:
            return super().prepare(input, tools)
        enriched = list(input)
        for index, message in enumerate(enriched):
            if not isinstance(message, TaskMessage) or message.observation is None:
                continue
            payload = _OBJECT.validate_json(message.text)
            observation = payload.get("observation")
            if not isinstance(observation, dict) or observation.get("baseline") is not True:
                continue
            observation["combat_history"] = _OBJECT.validate_json(self.history.to_json())
            enriched[index] = TextMessage(message.role, json.dumps(payload, ensure_ascii=False))
            break
        return super().prepare(tuple(enriched), tools)

    async def complete_prepared(
        self, prepared: PreparedRequest, *, preparation: RequestPreparation | None = None
    ) -> ModelReply:
        try:
            reply = await super().complete_prepared(prepared, preparation=preparation)
        except CapacityRejected:
            raise ModelUnavailable(FailureKind.CAPACITY) from None
        self.last_calls = reply.assistant.tool_calls
        if not reply.assistant.tool_calls:
            raise NoProposal("The response declared no tool; this trial is terminal unknown.")
        return reply


class FreshGame(CaseGame):
    def __init__(
        self, case: DecisionCase, observation: native.Observation, catalog: FrozenCatalog
    ) -> None:
        super().__init__(case, observation)
        self.frozen_catalog = catalog

    async def inspect_result(self, query: native.InspectQuery) -> InspectResult:
        history_query = (
            isinstance(query, native.MapQuery)
            and query.area is native.InspectArea.COMBAT_HISTORY
            and isinstance(self.observation.state, native.CombatState)
        )
        history_read: native.InspectCompleted | None = None
        for read in reversed(self.case.native_reads):
            if read.request != Inspect(query=query) or not isinstance(
                read.result, native.InspectCompleted
            ):
                continue
            owner = self.observation.location.owner
            if owner is None or not same_observation(
                owner, read.result.observation, self.observation
            ):
                continue
            if history_query:
                if read.result.observation != self.observation:
                    continue
                history = self.case.combat_history
                if (
                    not isinstance(read.result.data, native.HistoryData)
                    or (history is not None and history != read.result.data)
                    or (history_read is not None and history_read.data != read.result.data)
                ):
                    raise SnapshotIncomplete("The frozen combat history is inconsistent.")
                history_read = read.result
                continue
            return read.result.model_copy(update={"observation": self.observation})
        if history_read is not None:
            return history_read.model_copy(update={"observation": self.observation})
        if history_query:
            raise SnapshotIncomplete(
                "The snapshot has no exactly anchored combat history inspection."
            )
        raise SnapshotIncomplete("The snapshot has no matching current-state inspection.")

    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
        return GameReply(
            outcome=Cataloged(result=await self.frozen_catalog.query(request)), observations=()
        )

    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
        result = await self.inspect_result(query)
        return GameReply(outcome=Inspected(result=result), observations=(self.observation,))


async def sample_fresh(
    case: DecisionCase,
    client: Llm,
    *,
    catalog: FrozenCatalog,
    role: EvaluationRole,
    sample: int = 1,
    documents: FrozenDocuments | None = None,
    candidate_links: FrozenLinks | None = None,
    prompts: PromptReader | None = None,
) -> DecisionSample:
    if client.configuration.extra_retries:
        raise ValueError("Fixed trials require zero model transport retries.")
    started = monotonic()
    measured = FreshModel(client)
    choices: tuple[str, ...] = ()
    reason, error = "", None
    proposal: CapturedProposal | None = None
    proposal_status: ProposalStatus | None = None
    route_metrics: RouteMetrics | None = None
    verdict = DecisionVerdict.UNSUPPORTED
    try:
        task_event = next(event for event in reversed(case.events) if isinstance(event, TaskEvent))
        observation = task_event.observation
        run_id = observation.location.run_id
        if run_id is None:
            raise SnapshotIncomplete("The observation has no run identity.")
        if case.entry_documents_enabled and documents is None:
            raise SnapshotIncomplete("The case requires a frozen document snapshot.")
        if case.candidate_links_enabled and candidate_links is None:
            raise SnapshotIncomplete("The case requires a frozen candidate-link snapshot.")
        owner = (
            native.DecisionOwner.COMBAT
            if role is EvaluationRole.COMBAT
            else native.DecisionOwner.RUN
        )
        game = FreshGame(case, observation, catalog)
        if role is EvaluationRole.COMBAT:
            if not isinstance(observation.state, native.CombatState):
                raise SnapshotIncomplete("The observation is not a current combat state.")
            history_result = await game.inspect_result(
                native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)
            )
            if not isinstance(history_result, native.InspectCompleted) or not isinstance(
                history_result.data, native.HistoryData
            ):
                raise SnapshotIncomplete("The snapshot has no current combat history.")
            measured.history = history_result.data
        execution = Execution(
            game, game_instance_id=observation.location.game_instance_id, run_id=run_id
        )

        async def materials(current: native.Observation) -> tuple[DocumentView, ...]:
            if documents is None or not case.entry_documents_enabled:
                return ()
            return await entry_materials(
                execution, documents, MaterialRole(role.value), current, case.configured_guidance
            )

        fights = FightHistory()
        fights.recent = task_event.state.fights
        notes = Notes("")
        tools = ToolCatalog(
            owner,
            observe=execution.observe,
            inspect=game.inspect_result,
            catalog=catalog.query,
            supplements=documents,
            skills=documents,
            notes=notes if owner is native.DecisionOwner.RUN else None,
            fights=fights,
            focused_deck=role is EvaluationRole.DECK,
            minimal=case.minimal,
        )
        tools.deck_stats = CaseStatistics(task_event.state, observation)
        selected_prompts = prompts or PackagedPrompts()
        if role is EvaluationRole.COMBAT:
            agent: CaptureDecision = CombatProbe(
                measured,
                tools,
                ToolRecoveryBudget(),
                opening_notes=NotesView(),
                prompts=selected_prompts,
                materials=materials,
                user_input=UserInputQueue() if case.viewer else None,
            )
        elif role is EvaluationRole.DECK:
            agent = DeckProbe(
                lambda: measured,
                tools,
                ToolRecoveryBudget(),
                offer_ref=case.decision_offer,
                candidate_links=candidate_links,
                user_input=UserInputQueue() if case.viewer else None,
                prompts=selected_prompts,
                materials=materials,
            )
        else:
            agent = RunProbe(
                lambda: measured,
                tools,
                ToolRecoveryBudget(),
                candidate_links=candidate_links,
                user_input=UserInputQueue() if case.viewer else None,
                prompts=selected_prompts,
                materials=materials,
            )
        observation = agent.focus_observation(observation)
        await agent.prepare_fresh(observation)
        route = native.RouteKey.model_validate_json(observation.location.to_json())
        try:
            await Agent.decide(agent, ActTask(route), observation)
            raise NoProposal("The decision loop returned without an action proposal.")
        except Captured as captured:
            proposal, arguments, reason = _proposal_details(captured.call)
            proposal_status = captured.status
            if captured.error:
                prefix = (
                    "Proposal legality unknown"
                    if captured.status is ProposalStatus.UNKNOWN
                    else "Invalid action proposal"
                )
                error = f"{prefix}: {captured.error}"
            else:
                route_metrics = agent.route_metrics
                if captured.call.name in (ToolName.RUN_DECK_CHOICE, ToolName.RUN_ACT) and (
                    case.labels.good or case.labels.bad
                ):
                    choices = choices_for(decision_input(captured.call), observation)
                    verdict = score(case.labels, choices)
                else:
                    action = {key: value for key, value in arguments.items() if key != "reason"}
                    choices = (json.dumps({"tool": captured.call.name, **action}, sort_keys=True),)
                    verdict = DecisionVerdict.UNLABELED
    except (
        SnapshotIncomplete,
        ExtractionError,
        NoProposal,
        DecisionInterrupted,
        ModelUnavailable,
    ) as failure:
        error = str(failure)
        if proposal is None:
            pending = next(
                (
                    call
                    for call in measured.last_calls
                    if call.name in CaptureDecision.all_decisions
                ),
                None,
            )
            if pending is not None:
                proposal, _, reason = _proposal_details(pending)
                proposal_status = ProposalStatus.UNKNOWN
    return DecisionSample(
        source=case.source,
        sample=sample,
        verdict=verdict,
        choices=choices,
        reason=reason,
        error=error,
        proposal=proposal,
        proposal_status=proposal_status,
        route_metrics=route_metrics,
        requests=tuple(measured.sizes),
        usage=tuple(measured.usage),
        elapsed_seconds=round(monotonic() - started, 3),
        raw_calls=tuple(measured.calls),
        request_bodies=tuple(measured.bodies),
        model_replies=tuple(measured.replies),
        attempts=tuple(measured.attempts),
    )


def _proposal_details(call: ToolCall) -> tuple[CapturedProposal, dict[str, JsonValue], str]:
    proposal = CapturedProposal(tool=ToolName(call.name), arguments=call.arguments)
    root_fields: list[tuple[str, object]] = []

    def report_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal root_fields
        # The JSON decoder visits child objects before their parent.
        root_fields = pairs
        return dict(pairs)

    try:
        arguments = _OBJECT.validate_python(
            json.loads(call.arguments, object_pairs_hook=report_fields), strict=True
        )
    except (ValueError, ValidationError):
        arguments = {}
    value = arguments.get("reason") if sum(key == "reason" for key, _ in root_fields) == 1 else None
    return proposal, arguments, value if isinstance(value, str) else ""


def _verify_initial_selection(step: native.NativeStep, observation: native.Observation) -> None:
    if not isinstance(step, native.Select):
        if observation.input_readiness is native.InputReadiness.SELECTION:
            raise ExtractionError("The current selection must be completed first.")
        return
    selection = observation.state.selection
    if selection is None:
        if isinstance(observation.state, native.CombatState):
            # The Mod can accept a matching previously auto-resolved selection.
            raise SnapshotIncomplete("The frozen snapshot has no auto-selection state.")
        raise ExtractionError("No current selection is available.")
    if not set(step.refs) <= set(selection.candidates) or not (
        selection.min <= len(step.refs) <= selection.max
    ):
        raise ExtractionError("Selection is not currently legal.")


def _verify_run_action(parameters: RunActInput, observation: native.Observation) -> None:
    state = observation.state
    steps = parameters.root.steps
    if not isinstance(state, native.RunState):
        raise SnapshotIncomplete("The frozen state cannot verify this action.")
    if (
        observation.location.owner is not native.DecisionOwner.RUN
        or state.control is not native.StateControl.READY
        or observation.input_readiness
        not in (native.InputReadiness.READY, native.InputReadiness.SELECTION)
    ):
        raise ExtractionError("The Run state is not ready for an action.")
    _verify_initial_selection(steps[0], observation)
    if len(steps) != 1:
        raise SnapshotIncomplete("The frozen state cannot verify this action batch.")
    step = steps[0]
    if isinstance(step, native.Select):
        return
    if isinstance(step, native.Interact):
        action = next((item for item in state.actions if item.ref == step.action), None)
        if action is None or not action.enabled or action.destination is not MISSING:
            raise ExtractionError("Interaction is not currently legal.")
        return
    raise SnapshotIncomplete("The frozen state cannot verify this action.")
