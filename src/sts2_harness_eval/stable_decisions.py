"""Evaluate a fresh decision against an immutable observation without game writes."""

import json
from enum import StrEnum
from time import monotonic

from pydantic import JsonValue, TypeAdapter, ValidationError

from sts2_harness.agent import Agent
from sts2_harness.client import models as native
from sts2_harness.client.http import InspectResult
from sts2_harness.combat import CombatAgent
from sts2_harness.context import Context, same_observation
from sts2_harness.decisions import ActionProposal, ActTask, DecisionTask, LocalProgress
from sts2_harness.deck_role import DeckAgent
from sts2_harness.entry_materials import entry_materials
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.fights import FightHistory
from sts2_harness.game import Cataloged, GameReply, Inspect, Inspected
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    CapacityRejected,
    FailureKind,
    ModelReply,
    ModelUnavailable,
    PreparedRequest,
    RequestPreparation,
    ToolCall,
)
from sts2_harness.note_values import NotesView
from sts2_harness.notes import Notes
from sts2_harness.prompts import PackagedPrompts, PromptReader
from sts2_harness.results import QueryToolResult, ReadCompleted
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog, ToolName, unique_fields
from sts2_harness.user_input import UserInputQueue
from sts2_harness_eval.decision_cases import (
    DecisionCase,
    DecisionSample,
    DecisionVerdict,
    TaskEvent,
    score,
)
from sts2_harness_eval.decision_extract import ExtractionError, choices_for
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
    def __init__(self, call: ToolCall, error: str | None = None) -> None:
        self.call = call
        self.error = error
        super().__init__(error or "First proposal captured before execution.")


class CaptureDecision(Agent):
    decision_names: frozenset[ToolName]
    all_decisions = frozenset(
        (ToolName.RUN_ACT, ToolName.RUN_NEXT, ToolName.RUN_DECK_CHOICE, ToolName.COMBAT_ACT)
    )
    _tools: ToolCatalog

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
        raise Captured(first, "Invalid action layout.")

    async def _dispatch(
        self, call: ToolCall, task: DecisionTask, observation: native.Observation
    ) -> ActionProposal | QueryToolResult | LocalProgress:
        if call.name in self.all_decisions:
            definition = next(
                (item for item in self._tools.definitions if item.name == call.name), None
            )
            try:
                if definition is None or call.name not in self.decision_names:
                    raise ValueError("The action is unavailable to this role.")
                json.loads(call.arguments, object_pairs_hook=unique_fields)
                definition.parameters.model_validate_json(call.arguments, strict=True)
            except (ValueError, ValidationError) as error:
                raise Captured(call, str(error)) from error
            raise Captured(call)
        return await super()._dispatch(call, task, observation)


class RunProbe(CaptureDecision, RunAgent):
    decision_names = frozenset((ToolName.RUN_ACT, ToolName.RUN_NEXT))

    def focus_observation(self, observation: native.Observation) -> native.Observation:
        return self._reward_view(observation)


class CombatProbe(CaptureDecision, CombatAgent):
    decision_names = frozenset((ToolName.COMBAT_ACT,))


class DeckProbe(CaptureDecision, DeckAgent):
    decision_names = frozenset((ToolName.RUN_DECK_CHOICE,))

    def focus_observation(self, observation: native.Observation) -> native.Observation:
        return self._reward_view(observation)


class FreshModel(MeasuredModel):
    async def complete_prepared(
        self, prepared: PreparedRequest, *, preparation: RequestPreparation | None = None
    ) -> ModelReply:
        try:
            reply = await super().complete_prepared(prepared, preparation=preparation)
        except CapacityRejected:
            raise ModelUnavailable(FailureKind.CAPACITY) from None
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
            return read.result.model_copy(update={"observation": self.observation})
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
        execution = Execution(
            game, game_instance_id=observation.location.game_instance_id, run_id=run_id
        )
        supplied = (
            await entry_materials(
                execution, documents, owner, observation, case.configured_guidance
            )
            if documents is not None and case.entry_documents_enabled
            else ()
        )

        async def materials(current: native.Observation) -> tuple[DocumentView, ...]:
            return supplied

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
            if captured.error:
                error = f"Invalid action proposal: {captured.error}"
            else:
                arguments = _OBJECT.validate_json(captured.call.arguments)
                value = arguments.get("reason")
                reason = value if isinstance(value, str) else ""
                if captured.call.name in (ToolName.RUN_DECK_CHOICE, ToolName.RUN_ACT) and (
                    case.labels.good or case.labels.bad
                ):
                    choices = choices_for(captured.call, observation)
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
    return DecisionSample(
        source=case.source,
        sample=sample,
        verdict=verdict,
        choices=choices,
        reason=reason,
        error=error,
        requests=tuple(measured.sizes),
        usage=tuple(measured.usage),
        elapsed_seconds=round(monotonic() - started, 3),
        raw_calls=tuple(measured.calls),
        request_bodies=tuple(measured.bodies),
        model_replies=tuple(measured.replies),
        attempts=tuple(measured.attempts),
    )
