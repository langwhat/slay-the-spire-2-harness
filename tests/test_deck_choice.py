import asyncio
import json
from contextlib import closing
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue, TypeAdapter
from test_combat import reply
from test_lifecycle import terminal
from test_mod_client import CARD, PREVIEW, RUN
from test_responses import configuration
from test_run import handoff

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.context.models import RunActHandoffContent
from sts2_harness.deck_role import DeckAgent
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.fights import Fight, FightKind
from sts2_harness.game import RunEnded, RunEndReason
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.journal_models import AutomaticSnapshot, ResultSnapshot, RunTaskSnapshot
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.loop import DefaultFlow
from sts2_harness.note_values import NotesState
from sts2_harness.notes import Notes
from sts2_harness.results import ActionCompleted, ActionStopped, ActionUnknown, ReadCompleted
from sts2_harness.tools import ToolCatalog, ToolName
from sts2_harness.user_input import UserInputQueue
from sts2_harness.user_input_values import UserInput
from sts2_harness_eval.decision_cases import DecisionLabels, DecisionSource
from sts2_harness_eval.decision_extract import ExtractionError, extract_case


class RewardGame:
    def __init__(self, *, minimal: bool, unknown: bool = False) -> None:
        observation = native.Observation.model_validate_json(RUN)
        assert isinstance(observation.state, native.RunState)
        card = native.CardEntry.model_validate_json(CARD)
        self.minimal = minimal
        self.unknown = unknown
        self.skip_regular = False
        self.event_handoff = False
        self.event_take: tuple[str, ...] = ("gift",)
        self.event_followup = False
        self.take_guaranteed_first = False
        self.reward_reason: str | None = None
        self.event_min = 1
        self.event_max = 1
        self.inputs: list[dict[str, JsonValue]] = []
        self.steps: list[native.NativeStep] = []
        self.observation = observation.model_copy(
            update={
                "objects": {
                    ref: card.model_copy(update={"definition_id": "CARD." + ref.upper()})
                    for ref in ("owned", "local", "regular", "guaranteed", "shop", "gift", "shared")
                },
                "state": observation.state.model_copy(
                    update={
                        "screen": native.RunScreen.REWARDS,
                        "deck": ("owned",),
                        "offers": tuple(
                            native.Offer(
                                ref=ref,
                                kind=native.OfferKind.CARD,
                                contents=(ref,),
                                actions=("open-" + ref,),
                                requires_selection=ref == "regular",
                            )
                            for ref in ("local", "regular", "guaranteed")
                        )
                        + (
                            native.Offer(
                                ref="gold",
                                kind=native.OfferKind.GOLD,
                                contents=("shared",),
                                actions=("gold",),
                            ),
                        ),
                        "actions": tuple(
                            native.Action(ref="open-" + ref, label=ref, enabled=True)
                            for ref in ("local", "regular", "guaranteed")
                        )
                        + (
                            native.Action(ref="gold", label="Gold", enabled=True),
                            native.Action(
                                ref="proceed",
                                label="Continue",
                                enabled=True,
                                operation=native.ActionOperation.PROCEED,
                            ),
                        ),
                    }
                ),
                "map": native.PublicMap(bosses=(), nodes=(), legal_destinations=None),
                "generated": {
                    ref: (
                        native.GeneratedCard(
                            source=native.NamedDefinition(
                                name=ref + " generation", definition_id="CARD." + ref.upper()
                            ),
                            card=native.CardDefinitionPreview.model_validate_json(PREVIEW),
                        ),
                    )
                    for ref in ("owned", "local", "shared")
                },
                "mechanisms": tuple(
                    native.MechanismView(
                        kind=native.MechanismKind.OTHER,
                        definition_id="CARD." + ref.upper(),
                        name=ref + " mechanism",
                        text=ref + " rules",
                        owner=ref,
                    )
                    for ref in ("owned", "local", "shared")
                ),
            }
        )

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state":
            self.observation = self.observation.model_copy(
                update={
                    "stamp": self.observation.stamp.model_copy(
                        update={
                            "observation_sequence": self.observation.stamp.observation_sequence + 1
                        }
                    )
                }
            )
        obs = self.observation
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "location": obs.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "running",
                    "receipt": None,
                    "input_readiness": obs.input_readiness.value,
                },
            )
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=obs
                ).to_json(),
            )
        assert request.url.path == "/action"
        step = native.StepRequest.model_validate_json(request.content)
        self.steps.append(step.step)
        state = obs.state
        assert isinstance(state, native.RunState)
        update: dict[str, object] = {}
        unknown = False
        if isinstance(step.step, native.Select):
            update = {"deck": (*state.deck, *step.step.refs), "selection": None}
            if state.screen is native.RunScreen.REWARDS:
                if step.step.refs or not self.skip_regular:
                    update.update(
                        offers=tuple(o for o in state.offers if o.ref != "regular"),
                        actions=tuple(a for a in state.actions if a.ref != "open-regular"),
                    )
                unknown = self.unknown
            else:
                assert state.screen in (native.RunScreen.EVENT, native.RunScreen.SELECTION)
                update["screen"] = native.RunScreen.EVENT
                if self.event_followup and step.step.refs == ("gift",):
                    assert state.selection is not None
                    update["screen"] = native.RunScreen.SELECTION
                    update["selection"] = state.selection.model_copy(
                        update={"prompt": "Next event card", "candidates": ("shared",)}
                    )
                    self.event_take = ("shared",)
        else:
            assert isinstance(step.step, native.Interact)
            action = step.step.action
            if action == "open-regular":
                update = {
                    "selection": native.Selection(
                        prompt="Choose",
                        source=None,
                        min=0,
                        max=1,
                        candidates=("regular",),
                        selected=(),
                    )
                }
            elif action == "open-guaranteed":
                update = {
                    "deck": (*state.deck, "guaranteed"),
                    "offers": tuple(o for o in state.offers if o.ref != "guaranteed"),
                    "actions": tuple(a for a in state.actions if a.ref != "open-guaranteed"),
                }
            elif action == "gold":
                update = {
                    "offers": tuple(o for o in state.offers if o.ref != "gold"),
                    "actions": tuple(a for a in state.actions if a.ref != "gold"),
                }
            elif action == "proceed":
                update = {
                    "screen": native.RunScreen.SHOP,
                    "offers": (
                        native.Offer(
                            ref="shop",
                            kind=native.OfferKind.CARD,
                            contents=("shop",),
                            actions=("buy",),
                            price=50,
                        ),
                    ),
                    "actions": (
                        native.Action(ref="buy", label="Buy", enabled=True),
                        native.Action(
                            ref="close",
                            label="Leave",
                            enabled=True,
                            operation=native.ActionOperation.CLOSE_SHOP,
                        ),
                    ),
                }
            elif action == "buy":
                update = {
                    "deck": (*state.deck, "shop"),
                    "offers": (),
                    "actions": tuple(a for a in state.actions if a.ref == "close"),
                }
            elif action == "close":
                update = {
                    "screen": native.RunScreen.SELECTION
                    if self.event_handoff
                    else native.RunScreen.EVENT,
                    "offers": (),
                    "actions": (native.Action(ref="finish", label="Finish", enabled=True),),
                    "selection": native.Selection(
                        prompt="Event card",
                        source=None,
                        min=self.event_min,
                        max=self.event_max,
                        candidates=("gift", "shared")
                        if self.event_handoff and not self.event_followup
                        else ("gift",),
                        selected=(),
                    ),
                }
            else:
                assert action == "finish"
                self.observation = terminal(obs)
                return httpx.Response(
                    200,
                    json={
                        "operation_id": step.operation_id,
                        "status": "completed",
                        "boundary": "terminal",
                    },
                )
        changed = state.model_copy(update=update)
        readiness = (
            native.InputReadiness.SELECTION
            if changed.selection is not None
            else native.InputReadiness.READY
        )
        self.observation = obs.model_copy(update={"state": changed, "input_readiness": readiness})
        return httpx.Response(
            200,
            json={
                "operation_id": step.operation_id,
                "status": "unknown" if unknown else "completed",
                **(
                    {
                        "error": {
                            "code": "dispatch_failed",
                            "message": "Selection effect unconfirmed.",
                        }
                    }
                    if unknown
                    else {"boundary": readiness.value}
                ),
            },
        )

    def model(self, request: httpx.Request) -> httpx.Response:
        body = TypeAdapter(dict[str, JsonValue]).validate_json(request.content)
        self.inputs.append(body)
        tools = body["tools"]
        assert isinstance(tools, list)
        names: set[str] = set()
        for tool in tools:
            assert isinstance(tool, dict)
            name = tool["name"]
            assert isinstance(name, str)
            names.add(name)
        state = self.observation.state
        assert isinstance(state, native.RunState)
        index = len(self.inputs)
        calls: list[tuple[str, str, str]] = []
        if "run_deck_choice" in names:
            assert names == {"run_deck_choice"}
            messages = body["input"]
            assert isinstance(messages, list)
            choice: dict[str, JsonValue] | None = None
            for message in reversed(messages):
                if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    continue
                content = message["content"]
                assert isinstance(content, str)
                if not content.startswith("{"):
                    continue
                task = TypeAdapter(dict[str, JsonValue]).validate_json(content)
                observation = task.get("observation")
                if isinstance(observation, dict) and isinstance(observation.get("choice"), dict):
                    candidate = observation["choice"]
                    assert isinstance(candidate, dict)
                    choice = candidate
                    break
            assert choice is not None
            offers = choice.get("offers", [])
            assert isinstance(offers, list)
            plans: list[dict[str, object]] = []
            for offered in offers:
                assert isinstance(offered, dict) and isinstance(offered["ref"], str)
                offer = offered["ref"]
                cards = offered["candidates"]
                assert isinstance(cards, list) and cards
                card = cards[0]
                skip = offer == "local" or (offer == "regular" and self.skip_regular)
                args: dict[str, object] = {
                    "offer": offer,
                    "choice": "skip" if skip else "take",
                    "reason": self.reward_reason or "Measured " + offer,
                }
                if not skip:
                    args["card"] = card["ref"] if isinstance(card, dict) else card
                plans.append(args)
            if not offers:
                args = {
                    "choice": "take" if self.event_take else "skip",
                    "reason": "Event addition.",
                }
                if self.event_take:
                    args["card"] = list(self.event_take)
                plans.append(args)
            if self.take_guaranteed_first:
                plans.sort(key=lambda plan: plan.get("offer") == "local")
                plans.sort(key=lambda plan: plan.get("offer") != "guaranteed")
            selection = choice.get("selection")
            if isinstance(selection, dict):
                candidates = selection["candidates"]
                assert isinstance(candidates, list)
                active = [item["ref"] if isinstance(item, dict) else item for item in candidates]
                plans.sort(key=lambda plan: plan.get("card") not in active)
            calls.append((f"choice-{index}", "run_deck_choice", json.dumps({"choices": plans})))
        elif state.screen is native.RunScreen.REWARDS:
            if not self.minimal:
                calls.append(
                    (f"notes-{index}", "run_notes", '{"mode":"write","other":"Run shared note."}')
                )
            calls.append(
                (f"gold-{index}", "run_act", '{"steps":[{"kind":"interact","action":"gold"}]}')
            )
        elif state.screen is native.RunScreen.SHOP:
            calls.append(
                (
                    f"shop-{index}",
                    "run_act",
                    '{"steps":[{"kind":"interact","action":"buy"},{"kind":"interact","action":"close"}]}',
                )
            )
        else:
            assert state.screen is native.RunScreen.EVENT
            calls.append(
                (
                    f"event-{index}",
                    "run_act",
                    (
                        '{"steps":[{"kind":"interact","action":"finish"}]}'
                        if self.event_handoff
                        else '{"steps":[{"kind":"select","refs":["gift"]},'
                        '{"kind":"interact","action":"finish"}]}'
                    ),
                )
            )
        return httpx.Response(
            200, content=reply(*calls), headers={"Content-Type": "text/event-stream"}
        )


@pytest.mark.parametrize("minimal", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
def test_reward_roles_share_state_and_preserve_real_completion_evidence(
    tmp_path: Path,
    minimal: bool,
    unknown: bool,
) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=minimal, unknown=unknown)
        owned = game.observation.objects["owned"]
        assert isinstance(owned, native.CardEntry)
        game.observation = game.observation.model_copy(
            update={
                "objects": {
                    **game.observation.objects,
                    "owned": owned.model_copy(update={"cost": native.ResourceCosts(energy=2)}),
                }
            }
        )
        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="rewards",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(other="" if minimal else "Existing Run note."),
                minimal=minimal,
            ),
            archive,
            model=Llm(
                configuration(tmp_path, LLM_REASONING_EFFORT="low"),
                transport=httpx.MockTransport(game.model),
            ),
        ) as session:
            ended = await session.run()
            assert ended.reason is RunEndReason.TERMINAL
            assert session.notes.read().other == ("" if minimal else "Run shared note.")
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )
        deck_count = 2 if unknown else 1
        assert [body.get("prompt_cache_key") for body in game.inputs] == (
            ["rewards:deck"] * deck_count + ["rewards:run"] * 3
        )
        with closing(Journal.open(archive)) as journal:
            completions = journal.card_choices()
            assert len(completions) == 3
            assert [c.stream for c in completions] == ["deck:1"] * 3
            assert [c.batch_index for c in completions] == [1, 2, 1 if unknown else 3]
            assert (completions[0].call_id == completions[2].call_id) is not unknown
            assert (
                isinstance(completions[0].outcome, ReadCompleted) and completions[0].local_decline
            )
            assert completions[0].before == completions[0].after
            assert isinstance(completions[1].outcome, ActionUnknown) is unknown
            assert completions[2].before.state.deck == ("owned", "regular")
            assert completions[2].after is not None
            stats, _ = session.deck_stats.view(completions[2].after)
            assert stats["added"] == (1 if unknown else 2) and stats["skipped"] == 1
            requests = journal.requests()
            for request in requests:
                reply = request.attempts[-1].reply
                assert reply is not None and reply.requested_effort == "low"
            assert [r.stream for r in requests] == ["deck:1"] * deck_count + ["run:1"] * 3
            assert [step.action for step in game.steps if isinstance(step, native.Interact)] == [
                "open-regular",
                "open-guaranteed",
                "gold",
                "proceed",
                "buy",
                "close",
                "finish",
            ]
            first_deck = requests[0]
            task = next(m for m in first_deck.input if isinstance(m, RunTaskSnapshot))
            payload = json.loads(task.text)["observation"]
            assert "map" not in payload
            assert "route" not in payload["progress"]
            assert payload["deck_stats"]["circulating_avg_energy"] == 2.0
            assert [offer["ref"] for offer in payload["choice"]["offers"]] == [
                "local",
                "regular",
                "guaranteed",
            ]
            first_run = requests[deck_count]
            body_input = game.inputs[deck_count]["input"]
            assert isinstance(body_input, list)
            texts = [
                message["content"]
                for message in body_input
                if isinstance(message, dict) and isinstance(message.get("content"), str)
            ]
            first_run_text = "\n".join(text for text in texts if isinstance(text, str))
            assert '"circulating_avg_energy"' not in first_run_text
            assert "local mechanism" not in first_run_text
            assert "CARD.LOCAL" not in first_run_text
            assert "owned mechanism" in first_run_text
            assert "shared mechanism" in first_run_text
            assert '"generated"' in first_run_text
            reports = [m for m in first_run.input if isinstance(m, AutomaticSnapshot)]
            assert [m.content.automatic_progress.label for m in reports] == [
                "Skip",
                "CARD.REGULAR",
                "CARD.GUARANTEED",
            ]
            assert isinstance(reports[0].content.outcome, ActionCompleted)
            assert reports[0].content.outcome.completed_steps == 0
            assert isinstance(reports[1].content.outcome, ActionUnknown) is unknown
            assert [report.content.automatic_progress.reason for report in reports] == [
                "Measured local",
                "Measured regular",
                "Measured guaranteed",
            ]
            assert all(report.content.observation is None for report in reports[:-1])
            observations = [
                json.loads(text).get("observation")
                for text in texts
                if isinstance(text, str) and text.startswith("{")
            ]
            decks = [state["deck"] for state in observations if state and "deck" in state]
            assert len(decks) == 1
            assert {card["id"] for card in decks[0]} == {
                "CARD.OWNED",
                "CARD.REGULAR",
                "CARD.GUARANTEED",
            }
            if not minimal:
                assert "Existing Run note." in task.text
            if not unknown:
                source = DecisionSource(
                    round="fixture",
                    seed="seed",
                    floor=1,
                    model_sequence=first_deck.sequence,
                    archive=archive,
                )
                with pytest.raises(ExtractionError, match="one recorded card reward choice"):
                    await extract_case(source, DecisionLabels(comment="fixture"))

    asyncio.run(scenario())


def test_reward_groups_display_the_same_reason_at_each_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=True)
        reason = "保留卡组的过牌效率。"
        game.reward_reason = reason
        events: list[str] = []

        def mod(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/display":
                message = TypeAdapter(dict[str, JsonValue]).validate_json(request.content)
                assert isinstance(message["reason"], str)
                events.append(message["reason"])
                return httpx.Response(200, json={"status": "shown"})
            if request.url.path == "/action":
                step = native.StepRequest.model_validate_json(request.content).step
                events.append(step.action if isinstance(step, native.Interact) else "select")
            return game.mod(request)

        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="repeated-reward-reasons",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
            display_reasons=True,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL

        assert events[:6] == [reason, reason, "open-regular", "select", reason, "open-guaranteed"]

    asyncio.run(scenario())


def test_reward_candidate_change_stops_stale_choice_and_preserves_prior_take(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=False)
        game.take_guaranteed_first = True
        archive = tmp_path / "archive.sqlite"

        def mod(request: httpx.Request) -> httpx.Response:
            response = game.mod(request)
            if request.url.path != "/action":
                return response
            current = game.observation
            state = current.state
            step = native.StepRequest.model_validate_json(request.content).step
            if isinstance(step, native.Interact) and step.action == "open-regular":
                assert isinstance(state, native.RunState) and state.selection is not None
                regular = next(offer for offer in state.offers if offer.ref == "regular")
                changed = regular.model_copy(update={"contents": ("shared",)})
                overlay = changed.model_copy(update={"ref": "transient-overlay"})
                game.observation = current.model_copy(
                    update={
                        "state": state.model_copy(
                            update={
                                "offers": (
                                    overlay,
                                    *(changed if o == regular else o for o in state.offers),
                                ),
                                "selection": state.selection.model_copy(
                                    update={"candidates": ("shared",)}
                                ),
                            }
                        )
                    }
                )
            elif isinstance(step, native.Select) and step.refs == ("shared",):
                assert isinstance(state, native.RunState)
                game.observation = current.model_copy(
                    update={
                        "state": state.model_copy(
                            update={
                                "offers": tuple(
                                    o for o in state.offers if o.ref != "transient-overlay"
                                ),
                            }
                        )
                    }
                )
            return response

        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="changed-reward",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        assert [step.action for step in game.steps if isinstance(step, native.Interact)][:2] == [
            "open-guaranteed",
            "open-regular",
        ]
        assert [step.refs for step in game.steps if isinstance(step, native.Select)] == [
            ("shared",),
            ("gift",),
        ]
        with closing(Journal.open(archive)) as journal:
            first, stale, resolved, local = journal.card_choices()
            assert [c.stream for c in (first, stale, resolved, local)] == ["deck:1"] * 4
            assert first.call_id == stale.call_id and resolved.call_id == local.call_id
            assert [first.offer_ref, stale.offer_ref, resolved.offer_ref] == [
                "guaranteed",
                "regular",
                "regular",
            ]
            assert isinstance(first.outcome, ActionCompleted)
            assert isinstance(stale.outcome, ActionStopped) and stale.outcome.completed_steps == 1
            assert isinstance(resolved.outcome, ActionCompleted)
            assert resolved.after is not None
            assert resolved.after.state.deck == ("owned", "guaranteed", "shared")
            second_request = journal.requests()[1]
            task = next(m for m in reversed(second_request.input) if isinstance(m, RunTaskSnapshot))
            offers = json.loads(task.text)["observation"]["choice"]["offers"]
            assert [offer["ref"] for offer in offers] == ["local", "regular"]
            assert any(
                "The card reward groups or their available choices changed." in m.text
                for m in second_request.input
                if isinstance(m, ResultSnapshot)
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("full", (False, True))
def test_battle_reward_order_and_full_potion_handoff(tmp_path: Path, full: bool) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=True)
        observation = game.observation
        state = observation.state
        assert isinstance(state, native.RunState)
        relic = native.RelicEntry(
            kind=native.ObjectKind.RELIC, definition_id="RELIC.ONE", name="Relic"
        )
        potion = native.PotionEntry(
            kind=native.ObjectKind.POTION,
            definition_id="POTION.ONE",
            name="Potion",
            usable=False,
            needs_target=False,
            target_type=None,
            usage=None,
        )
        offers = (
            native.Offer(
                ref="offered-potion",
                kind=native.OfferKind.POTION,
                contents=("new-potion",),
                actions=("claim-potion",),
            ),
            native.Offer(
                ref="offered-relic",
                kind=native.OfferKind.RELIC,
                contents=("reward-relic",),
                actions=("claim-relic",),
            ),
        )
        game.observation = observation.model_copy(
            update={
                "objects": {
                    **observation.objects,
                    "reward-relic": relic,
                    "new-potion": potion,
                    "old-potion": potion,
                },
                "state": state.model_copy(
                    update={
                        "progress": state.progress.model_copy(update={"room_type": "monster"}),
                        "player": state.player.model_copy(
                            update={"empty_potion_slot_count": int(not full)}
                        ),
                        "potions": ("old-potion",) if full else (),
                        "offers": (*state.offers, *offers),
                        "actions": (
                            *state.actions,
                            native.Action(ref="claim-potion", label="领取药水", enabled=True),
                            native.Action(ref="claim-relic", label="领取遗物", enabled=True),
                        ),
                    }
                ),
            }
        )

        def mod(request: httpx.Request) -> httpx.Response:
            if request.url.path != "/action":
                return game.mod(request)
            wire = native.StepRequest.model_validate_json(request.content)
            step = wire.step
            if not isinstance(step, native.Interact) or step.action not in {
                "claim-potion",
                "claim-relic",
                "drop-potion",
            }:
                return game.mod(request)
            current = game.observation
            current_state = current.state
            assert isinstance(current_state, native.RunState)
            game.steps.append(step)
            change: dict[str, object] = {}
            if step.action == "claim-relic":
                change["relics"] = ("reward-relic",)
            elif step.action == "drop-potion":
                change["potions"] = ()
                change["player"] = current_state.player.model_copy(
                    update={"empty_potion_slot_count": 1}
                )
            else:
                assert current_state.player.empty_potion_slot_count == 1
                change["potions"] = ("new-potion",)
                change["player"] = current_state.player.model_copy(
                    update={"empty_potion_slot_count": 0}
                )
            change["offers"] = tuple(
                offer for offer in current_state.offers if step.action not in offer.actions
            )
            change["actions"] = tuple(a for a in current_state.actions if a.ref != step.action)
            game.observation = current.model_copy(
                update={"state": current_state.model_copy(update=change)}
            )
            return httpx.Response(
                200,
                json={
                    "operation_id": wire.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        def model(request: httpx.Request) -> httpx.Response:
            current = game.observation.state
            assert isinstance(current, native.RunState)
            body = TypeAdapter(dict[str, JsonValue]).validate_json(request.content)
            if (
                full
                and current.screen is native.RunScreen.REWARDS
                and body.get("prompt_cache_key") == "battle-rewards:run"
            ):
                assert [
                    offer.ref for offer in current.offers if offer.kind is native.OfferKind.POTION
                ] == ["offered-potion"]
                game.inputs.append(body)
                return httpx.Response(
                    200,
                    content=reply(
                        (
                            "replace-potion",
                            "run_act",
                            '{"steps":[{"kind":"interact","action":"drop-potion"},'
                            '{"kind":"interact","action":"claim-potion"}]}',
                        )
                    ),
                    headers={"Content-Type": "text/event-stream"},
                )
            return game.model(request)

        if full:
            current = game.observation.state
            assert isinstance(current, native.RunState)
            game.observation = game.observation.model_copy(
                update={
                    "state": current.model_copy(
                        update={
                            "actions": (
                                *current.actions,
                                native.Action(ref="drop-potion", label="弃掉药水", enabled=True),
                            )
                        }
                    )
                }
            )
        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="battle-rewards",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        interactions = [step.action for step in game.steps if isinstance(step, native.Interact)]
        expected = ["claim-relic", "gold"]
        if full:
            expected += ["open-regular", "open-guaranteed", "drop-potion", "claim-potion"]
        else:
            expected += ["claim-potion", "open-regular", "open-guaranteed"]
        assert interactions == [*expected, "proceed", "buy", "close", "finish"]
        assert game.inputs[0]["prompt_cache_key"] == "battle-rewards:deck"
        with closing(Journal.open(archive)) as journal:
            first_deck = journal.requests()[0]
            task = next(m for m in first_deck.input if isinstance(m, RunTaskSnapshot))
            assert "RELIC.ONE" in task.text
            if not full:
                assert "POTION.ONE" in task.text

    asyncio.run(scenario())


@pytest.mark.parametrize("minimal", [False, True])
def test_reward_http_input_includes_shared_battle_history(tmp_path: Path, minimal: bool) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=minimal)
        fights = (
            Fight(
                floor=1,
                kind=FightKind.NORMAL,
                hp_in=50,
                hp_out=46,
                damage=(),
                damage_one=(),
                incoming=(),
                hp_lost=(),
                potions=(),
            ),
        )
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="reward-history",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=minimal,
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            session.execution.fights.observe(game.observation)
            session.execution.fights.recent = fights
            await session.run()
        for body in game.inputs[:1]:
            messages = body["input"]
            assert isinstance(messages, list)
            observations: list[dict[str, JsonValue]] = []
            for message in messages:
                assert isinstance(message, dict)
                content = message.get("content")
                if not isinstance(content, str) or not content.startswith("{"):
                    continue
                payload = TypeAdapter(dict[str, JsonValue]).validate_json(content)
                observation = payload.get("observation")
                if isinstance(observation, dict):
                    observations.append(observation)
            assert observations[-1]["fights"] == [fight.model_dump(mode="json") for fight in fights]
            assert "progress.bosses_left" in json.dumps(body, ensure_ascii=False)

    asyncio.run(scenario())


def test_viewer_input_reaches_the_current_focused_role(tmp_path: Path) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=True)
        inputs = UserInputQueue()
        inputs.submit((UserInput("reward-input", "Current reward intervention."),))
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="viewer-reward",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
            user_input=inputs,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        assert inputs.delivered == ("reward-input",)
        assert "Current reward intervention." in json.dumps(game.inputs[0])
        assert "Current reward intervention." not in json.dumps(game.inputs[1:])

    asyncio.run(scenario())


@pytest.mark.parametrize("minimal", [False, True])
def test_reward_report_survives_automatic_travel_and_act_entry(
    tmp_path: Path, minimal: bool
) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=minimal)
        state = game.observation.state
        assert isinstance(state, native.RunState)
        game.observation = game.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "offers": state.offers[:1],
                        "actions": tuple(
                            a for a in state.actions if a.ref in ("open-local", "proceed")
                        ),
                    }
                )
            }
        )

        def mod(request: httpx.Request) -> httpx.Response:
            response = game.mod(request)
            if request.url.path == "/action":
                step = native.StepRequest.model_validate_json(request.content).step
                if isinstance(step, native.Interact) and step.action == "proceed":
                    current = game.observation
                    game.observation = current.model_copy(
                        update={
                            "location": current.location.model_copy(update={"node_id": "act2"}),
                            "state": current.state.model_copy(
                                update={
                                    "progress": current.state.progress.model_copy(update={"act": 2})
                                }
                            ),
                        }
                    )
            return response

        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="cross-act",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=minimal,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )
        with closing(Journal.open(archive)) as journal:
            requests = journal.requests()
            assert [record.stream for record in requests] == ["deck:1", "run:2", "run:2"]
            reports = [
                message for message in requests[1].input if isinstance(message, AutomaticSnapshot)
            ]
            assert any(
                report.content.automatic_progress.label == "Skip"
                and report.content.automatic_progress.reason == "Measured local"
                for report in reports
            )

    asyncio.run(scenario())


def test_local_decline_is_durable_when_no_later_model_request_exists(tmp_path: Path) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=True)
        state = game.observation.state
        assert isinstance(state, native.RunState)
        game.observation = game.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "offers": state.offers[:1],
                        "actions": tuple(
                            a for a in state.actions if a.ref in ("open-local", "proceed")
                        ),
                    }
                )
            }
        )

        def mod(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/action":
                step = native.StepRequest.model_validate_json(request.content)
                assert isinstance(step.step, native.Interact) and step.step.action == "proceed"
                game.steps.append(step.step)
                game.observation = terminal(game.observation)
                return httpx.Response(
                    200,
                    json={
                        "operation_id": step.operation_id,
                        "status": "completed",
                        "boundary": "terminal",
                    },
                )
            return game.mod(request)

        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="local-only",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )
        with closing(Journal.open(archive)) as journal:
            assert len(journal.requests()) == len(journal.card_choices()) == 1
            choice = journal.card_choices()[0]
            assert choice.local_decline and isinstance(choice.outcome, ReadCompleted)
            assert choice.before == choice.after
            source = DecisionSource(
                round="fixture",
                seed="seed",
                floor=1,
                model_sequence=journal.requests()[0].sequence,
                archive=archive,
            )
            case = await extract_case(source, DecisionLabels(comment="fixture"))
            assert case.recorded_choices == ("skip",)
            assert [step.action for step in game.steps if isinstance(step, native.Interact)] == [
                "proceed"
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("minimal", [False, True])
def test_completed_native_skip_is_not_reopened_and_preserves_run_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, minimal: bool
) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=minimal)
        game.skip_regular = True

        def model(request: httpx.Request) -> httpx.Response:
            assert len(game.inputs) < 7, "A completed skip must not ask again for the same offer."
            return game.model(request)

        archive = tmp_path / "archive.sqlite"
        pending = handoff("Preserve this battle review for Run.")

        class PendingFlow(DefaultFlow):
            async def run(self) -> RunEnded:
                self._pending = self._latest_handoff = pending
                return await super().run()

        monkeypatch.setattr("sts2_harness.session.DefaultFlow", PendingFlow)
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="native-skip",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=minimal,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )
        with closing(Journal.open(archive)) as journal:
            choices = journal.card_choices()
            assert [c.offer_ref for c in choices] == ["local", "regular", "guaranteed"]
            skipped = choices[1]
            assert not skipped.local_decline
            assert isinstance(skipped.outcome, ActionCompleted)
            assert skipped.outcome.completed_steps == 2
            assert skipped.after is not None
            assert skipped.before.state.deck == skipped.after.state.deck
            assert isinstance(skipped.after.state, native.RunState)
            assert skipped.after.state.selection is None
            assert any(o.ref == "regular" for o in skipped.after.state.offers)
            assert (
                sum(
                    isinstance(step, native.Interact) and step.action == "open-regular"
                    for step in game.steps
                )
                == 1
            )
            assert (
                sum(isinstance(step, native.Select) and not step.refs for step in game.steps) == 1
            )
            requests = journal.requests()
            assert [r.stream for r in requests] == [
                "deck:1",
                "run:1",
                "run:1",
                "run:1",
            ]
            task = next(m for m in requests[1].input if isinstance(m, RunTaskSnapshot))
            assert isinstance(task.content, RunActHandoffContent)
            assert task.content.battle_report == pending.report
            assert task.content.combat_review == pending.review
            reports = [m for m in requests[1].input if isinstance(m, AutomaticSnapshot)]
            assert [m.content.automatic_progress.label for m in reports] == [
                "Skip",
                "Skip",
                "CARD.GUARANTEED",
            ]
            stats, _ = session.deck_stats.view(skipped.after)
            assert stats["added"] == 1 and stats["skipped"] == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("minimal", [False, True])
def test_stopped_selection_keeps_focused_context_and_does_not_reopen_reward(
    tmp_path: Path,
    minimal: bool,
) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=minimal)
        rejected = False

        def mod(request: httpx.Request) -> httpx.Response:
            nonlocal rejected
            if request.url.path == "/action":
                step = native.StepRequest.model_validate_json(request.content)
                if (
                    isinstance(step.step, native.Select)
                    and step.step.refs == ("regular",)
                    and not rejected
                ):
                    rejected = True
                    game.steps.append(step.step)
                    return httpx.Response(
                        200,
                        json={
                            "operation_id": step.operation_id,
                            "status": "failed",
                            "error": {
                                "code": "dispatch_failed",
                                "message": "Paused before selection.",
                            },
                        },
                    )
            return game.mod(request)

        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="stopped",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=minimal,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )
        with closing(Journal.open(archive)) as journal:
            choices = journal.card_choices()
            assert len(choices) == 4
            assert [c.stream for c in choices] == ["deck:1"] * 4
            assert choices[1].outcome.status.value == "stopped"
            assert choices[2].outcome.status.value == "completed"
            assert (
                sum(
                    isinstance(s, native.Interact) and s.action == "open-regular"
                    for s in game.steps
                )
                == 1
            )
            retried = [r for r in journal.requests() if r.stream == "deck:1"][1]
            assert "Paused before selection." in " ".join(
                m.text for m in retried.input if isinstance(m, ResultSnapshot)
            )
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("taken", [("gift", "shared"), ()])
def test_event_choice_submits_all_selected_cards_once_and_reports_to_run(
    tmp_path: Path, taken: tuple[str, ...]
) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=False)
        game.event_handoff = True
        game.event_take = taken
        game.event_min = 0
        game.event_max = 2
        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="event-cards",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        with closing(Journal.open(archive)) as journal:
            completion = journal.card_choices()[-1]
            event_request = next(
                request for request in journal.requests() if request.stream == completion.stream
            )
            task = next(m for m in event_request.input if isinstance(m, RunTaskSnapshot))
            candidates = json.loads(task.text)["observation"]["choice"]["selection"]["candidates"]
            assert {card["id"] for card in candidates} == {"CARD.GIFT", "CARD.SHARED"}
            assert all("links" not in card for card in candidates)
            assert completion.offer_ref is None and completion.local_decline is False
            assert isinstance(completion.outcome, ActionCompleted)
            assert completion.after is not None
            assert completion.after.state.deck == (*completion.before.state.deck, *taken)
            selections = [step.refs for step in game.steps if isinstance(step, native.Select)]
            assert selections[-1] == taken
            final_request = journal.requests()[-1]
            reports = [
                m
                for m in final_request.input
                if isinstance(m, AutomaticSnapshot)
                and m.content.automatic_progress.reason == "Event addition."
            ]
            assert len(reports) == 1
            assert reports[0].content.automatic_progress.reason == "Event addition."

    asyncio.run(scenario())


def test_consecutive_unpublished_selectors_share_one_deck_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=False)
        game.event_handoff = True
        game.event_followup = True
        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="consecutive-rewards",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        with closing(Journal.open(archive)) as journal:
            first, second = journal.card_choices()[-2:]
            assert first.stream == second.stream == "deck:2"
            assert first.call_id != second.call_id
            assert first.card_ref == ("gift",) and second.card_ref == ("shared",)
            assert first.offer_ref is second.offer_ref is None
            requests = [request for request in journal.requests() if request.stream == first.stream]
            assert len(requests) == 2
            for request, expected in zip(requests, ("CARD.GIFT", "CARD.SHARED"), strict=True):
                task = next(m for m in reversed(request.input) if isinstance(m, RunTaskSnapshot))
                candidates = json.loads(task.text)["observation"]["choice"]["selection"][
                    "candidates"
                ]
                assert [card["id"] for card in candidates] == [expected]
            assert any(isinstance(message, ResultSnapshot) for message in requests[1].input)

    asyncio.run(scenario())


@pytest.mark.parametrize("has_map", (True, False))
def test_deck_guidance_uses_all_rewards_and_the_active_selection(
    tmp_path: Path, has_map: bool
) -> None:
    from sts2_harness.knowledge import MaterialRole, SubjectId
    from sts2_harness.materials import DocumentSource, FileDocuments

    game = RewardGame(minimal=False)
    state = game.observation.state
    assert isinstance(state, native.RunState)
    assert game.observation.map is not MISSING
    public_map = game.observation.map.model_copy(
        update={"bosses": (native.NamedDefinition(name="Boss", definition_id="ENCOUNTER.BOSS"),)}
    )
    game.observation = game.observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "selection": native.Selection(
                        prompt="Select",
                        source=None,
                        min=0,
                        max=1,
                        candidates=("local",),
                        selected=(),
                    )
                }
            ),
            "input_readiness": native.InputReadiness.SELECTION,
            "map": public_map if has_map else MISSING,
        }
    )
    sources: list[DocumentSource] = []
    for ref in ("owned", "local", "regular", "guaranteed"):
        path = tmp_path / ref
        path.write_text(f"RULE FOR {ref}")
        sources.append(
            DocumentSource(
                ref,
                ref,
                ref,
                path,
                subjects=(SubjectId(category="card", definition_id=f"CARD.{ref.upper()}"),),
                auto_roles=(MaterialRole.RUN, MaterialRole.DECK),
            )
        )
    path = tmp_path / "run-only"
    path.write_text("REWARD PROBABILITY RULE")
    sources.append(
        DocumentSource(
            "run-only",
            "run-only",
            "run-only",
            path,
            subjects=(SubjectId(category="character", definition_id=state.player.definition_id),),
            auto_roles=(MaterialRole.RUN,),
        )
    )
    path = tmp_path / "boss"
    path.write_text("PUBLIC BOSS STRATEGY")
    sources.append(
        DocumentSource(
            "boss",
            "boss",
            "boss",
            path,
            subjects=(SubjectId(category="encounter", definition_id="ENCOUNTER.BOSS"),),
            auto_roles=(MaterialRole.RUN, MaterialRole.DECK),
        )
    )
    inputs: list[dict[str, JsonValue]] = []
    inspections = 0

    class Captured(Exception):
        pass

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal inspections
        if request.url.path == "/inspect":
            inspections += 1
            return httpx.Response(
                200,
                content=native.InspectCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=game.observation,
                    data=native.MapData(map=public_map),
                ).to_json(),
            )
        return game.mod(request)

    def model(request: httpx.Request) -> httpx.Response:
        inputs.append(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
        raise Captured

    async def scenario() -> None:
        with pytest.raises(Captured):
            async with live_session(
                ModClient("http://mod", transport=httpx.MockTransport(mod)),
                Manifest(
                    materials=SessionMaterials(
                        entry_documents_enabled=True, act_guidance={}, battle_guidance=()
                    ),
                    recording_id="focused",
                    core_version="test",
                    game_instance_id="game",
                    run_id="run",
                    initial_notes=NotesState(),
                ),
                tmp_path / "focused.sqlite",
                model=Llm(configuration(tmp_path), transport=httpx.MockTransport(model)),
                documents=FileDocuments(tuple(sources)),
            ) as session:
                await session.run()
        body = json.dumps(inputs[0], ensure_ascii=False)
        assert "RULE FOR owned" in body and "RULE FOR local" in body
        assert "RULE FOR regular" in body and "RULE FOR guaranteed" in body
        assert "REWARD PROBABILITY RULE" not in body
        assert "PUBLIC BOSS STRATEGY" in body
        assert inspections == 0 if has_map else inspections > 0
        assert "CARD.REGULAR" in body and "CARD.GUARANTEED" in body
        messages = inputs[0]["input"]
        assert isinstance(messages, list)
        user = next(
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        )
        assert isinstance(user["content"], str)
        observation = json.loads(user["content"])["observation"]
        assert "map" not in observation
        candidates = observation["choice"]["selection"]["candidates"]
        assert len(candidates) == 1 and candidates[0]["ref"] == "local"
        with closing(Journal.open(tmp_path / "focused.sqlite")) as journal:
            from sts2_harness.journal_models import DocumentLookupResult

            lookups = [
                record.reply
                for record in journal.materials()
                if isinstance(record.reply, DocumentLookupResult)
            ]
            assert lookups
            assert all(doc.auto_roles is not MISSING for doc in lookups[0].documents)

    asyncio.run(scenario())


def test_deck_agent_rejects_a_run_tool_catalog() -> None:
    async def unused_observe() -> native.Observation:
        raise AssertionError

    async def unused_inspect(query: native.InspectQuery) -> native.InspectReply:
        raise AssertionError(query)

    async def unused_catalog(request: native.CatalogRequest) -> native.CatalogReply:
        raise AssertionError(request)

    def catalog(*, focused_deck: bool) -> ToolCatalog:
        return ToolCatalog(
            native.DecisionOwner.RUN,
            observe=unused_observe,
            inspect=unused_inspect,
            catalog=unused_catalog,
            notes=Notes(""),
            focused_deck=focused_deck,
        )

    def unused_model() -> ModelClient:
        raise AssertionError

    with pytest.raises(ValueError, match="focused Deck tool catalog"):
        DeckAgent(unused_model, catalog(focused_deck=False), ToolRecoveryBudget())
    focused = catalog(focused_deck=True)
    DeckAgent(unused_model, focused, ToolRecoveryBudget())
    assert [tool.name for tool in focused.definitions] == [ToolName.RUN_DECK_CHOICE]
