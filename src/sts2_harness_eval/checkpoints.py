"""Development checkpoint IO belongs to the evaluation owner, never the playing core."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Annotated, Literal, Self
from uuid import uuid4

import httpx2 as httpx
from pydantic import Field, TypeAdapter, model_validator

from sts2_harness.client import models as native

type Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type Nonnegative = Annotated[int, Field(ge=0)]


class ReloadTarget(native.WireModel):
    canonical_seed: native.Nonempty
    game_mode: native.Nonempty
    character: native.Nonempty
    ascension: Nonnegative
    act: Annotated[int, Field(ge=1)]
    floor: Nonnegative


class DeckCardKey(native.WireModel):
    owner_net_id: Nonnegative
    deck_index: Nonnegative


class KeyKind(StrEnum):
    CARD = "card"
    CREATURE = "creature"
    RELIC = "relic"
    POTION = "potion"
    MAP = "map"
    REWARD = "reward"


class RewardBindingKind(StrEnum):
    REWARD_OBJECT = "reward_object"
    CLAIM_ACTION = "claim_action"
    CHOICE_SCREEN = "choice_screen"
    CREATION_CARD = "creation_card"
    PICK_ACTION = "pick_action"
    ALTERNATIVE_ACTION = "alternative_action"
    PROCEED_ACTION = "proceed_action"
    CONTENT_OBJECT = "content_object"


class RewardAddress(native.WireModel):
    owner_net_id: Nonnegative
    native_rewards_set_index: Nonnegative | None
    binding_kind: RewardBindingKind
    creation_slot: Nonnegative | None
    native_option_id: native.Nonempty | None

    @model_validator(mode="after")
    def require_binding_fields(self) -> Self:
        parent_required = self.binding_kind is not RewardBindingKind.PROCEED_ACTION
        slot_required = self.binding_kind in (
            RewardBindingKind.CREATION_CARD,
            RewardBindingKind.PICK_ACTION,
        )
        option_required = self.binding_kind is RewardBindingKind.ALTERNATIVE_ACTION
        if (self.native_rewards_set_index is not None) != parent_required:
            raise ValueError("The reward parent index must match the binding kind.")
        if (self.creation_slot is not None) != slot_required:
            raise ValueError("The reward creation slot must match the binding kind.")
        if (self.native_option_id is not None) != option_required:
            raise ValueError("The native reward option must match the binding kind.")
        return self


class RewardKey(native.WireModel):
    kind: Literal[KeyKind.REWARD] = KeyKind.REWARD
    binding: RewardAddress


class CardKey(native.WireModel):
    kind: Literal[KeyKind.CARD] = KeyKind.CARD
    combat_id: Nonnegative | None
    deck: DeckCardKey | None

    @model_validator(mode="after")
    def require_identity(self) -> Self:
        if self.combat_id is None and self.deck is None:
            raise ValueError("A card requires an observed native identity.")
        return self


class CreatureKey(native.WireModel):
    kind: Literal[KeyKind.CREATURE] = KeyKind.CREATURE
    combat_id: Nonnegative


class RelicSlotKey(native.WireModel):
    kind: Literal[KeyKind.RELIC] = KeyKind.RELIC
    owner_net_id: Nonnegative
    slot: Nonnegative


class PotionSlotKey(native.WireModel):
    """A potion's position in the player's potion list; the native read carries no potion ids."""

    kind: Literal[KeyKind.POTION] = KeyKind.POTION
    slot: Nonnegative


class MapPointKey(native.WireModel):
    kind: Literal[KeyKind.MAP] = KeyKind.MAP
    act: Annotated[int, Field(ge=1)]
    row: int
    col: int


type NativeKey = Annotated[
    CardKey | CreatureKey | RelicSlotKey | PotionSlotKey | MapPointKey | RewardKey,
    Field(discriminator="kind"),
]


class CardIdentity(native.WireModel):
    ref: native.Nonempty
    combat_card_id: Nonnegative | None
    deck_card: DeckCardKey | None


class CreatureIdentity(native.WireModel):
    ref: native.Nonempty
    combat_id: Nonnegative | None


class MapIdentity(native.WireModel):
    ref: native.Nonempty
    act: int
    row: int
    col: int


class RelicIdentity(native.WireModel):
    ref: native.Nonempty
    owner_net_id: Nonnegative
    slot: Nonnegative
    # The counter shown while the relic's trigger visuals play, and the one shown at rest.
    counter_threshold: int | None = None
    counter_reset: int | None = None


class RewardIdentity(native.WireModel):
    ref: native.Nonempty
    binding: RewardAddress


class IdentityFacts(native.WireModel):
    cards: tuple[CardIdentity, ...]
    creatures: tuple[CreatureIdentity, ...]
    map_travel: tuple[MapIdentity, ...]
    relics: tuple[RelicIdentity, ...]
    reward_bindings: tuple[RewardIdentity, ...] = ()


class IdentityRead(native.WireModel):
    status: Literal[native.ResultStatus.COMPLETED]
    observation: native.Observation
    identities: IdentityFacts


class CheckpointErrorCode(StrEnum):
    DISABLED = "disabled"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    BUSY = "busy"
    NATIVE_REJECTED = "native_rejected"
    EXECUTION_UNCERTAIN = "execution_uncertain"


class CheckpointCause(native.WireModel):
    code: CheckpointErrorCode
    message: str
    native_reason: str | None = None


class CheckpointFile(native.WireModel):
    path: Path
    sha256: Sha256
    bytes: Annotated[int, Field(gt=0)]


class CheckpointState(native.WireModel):
    route: native.RouteKey
    native_run_instance_id: int
    native_revision: int
    target: ReloadTarget


class CheckpointKind(StrEnum):
    CAPTURE = "capture"
    LOAD = "load"
    STATUS = "status"


class CheckpointProof(native.WireModel):
    kind: Literal[CheckpointKind.CAPTURE, CheckpointKind.LOAD]
    state: CheckpointState
    file: CheckpointFile


class PendingCheckpoint(native.WireModel):
    status: Literal[native.ResultStatus.PENDING]
    command_id: str
    native_dispatched: bool


class CompletedCheckpoint(native.WireModel):
    status: Literal[native.ResultStatus.COMPLETED]
    command_id: str
    proof: CheckpointProof


class RejectedCheckpoint(native.WireModel):
    status: Literal[native.ResultStatus.REJECTED]
    command_id: str | None
    error: CheckpointCause


class UnknownCheckpoint(native.WireModel):
    status: Literal[native.ResultStatus.UNKNOWN]
    command_id: str
    native_dispatched: bool
    error: CheckpointCause


type CheckpointReply = Annotated[
    PendingCheckpoint | CompletedCheckpoint | RejectedCheckpoint | UnknownCheckpoint,
    Field(discriminator="status"),
]
_REPLY = TypeAdapter(CheckpointReply)


class CaptureRequest(native.WireModel):
    kind: Literal[CheckpointKind.CAPTURE] = CheckpointKind.CAPTURE
    command_id: str
    expected_route: native.RouteKey
    output_directory: Path


class LoadRequest(native.WireModel):
    kind: Literal[CheckpointKind.LOAD] = CheckpointKind.LOAD
    command_id: str
    checkpoint_path: Path
    checkpoint_sha256: Sha256
    target: ReloadTarget


class CheckpointStatus(native.WireModel):
    kind: Literal[CheckpointKind.STATUS] = CheckpointKind.STATUS
    command_id: str


class CheckpointUncertain(Exception):
    """The mutation may have happened; no new mutation may be sent."""


class CheckpointRejected(Exception):
    def __init__(self, cause: CheckpointCause) -> None:
        self.cause = cause
        super().__init__(cause.code.value)


class CheckpointClient:
    def __init__(
        self,
        url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=url, transport=transport, timeout=timeout_seconds, trust_env=False
        )
        self._timeout = timeout_seconds

    async def __aenter__(self) -> Self:
        await self._http.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._http.__aexit__(exc_type, exc, traceback)

    async def identity(self) -> IdentityRead:
        response = await self._http.get("/operator/checkpoint/identity")
        response.raise_for_status()
        return IdentityRead.model_validate_json(response.content)

    async def capture(self, route: native.RouteKey, output: Path) -> CheckpointProof:
        return await self._command(
            CaptureRequest(
                command_id=uuid4().hex, expected_route=route, output_directory=output.resolve()
            )
        )

    async def load(self, path: Path, sha256: str, target: ReloadTarget) -> CheckpointProof:
        return await self._command(
            LoadRequest(
                command_id=uuid4().hex,
                checkpoint_path=path.resolve(),
                checkpoint_sha256=sha256,
                target=target,
            )
        )

    async def _command(self, request: CaptureRequest | LoadRequest) -> CheckpointProof:
        async with asyncio.timeout(self._timeout):
            current: CaptureRequest | LoadRequest | CheckpointStatus = request
            while True:
                response = await self._http.post(
                    "/operator/checkpoint",
                    content=current.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )
                reply = _REPLY.validate_json(response.content)
                if isinstance(reply, RejectedCheckpoint):
                    if reply.command_id not in (None, request.command_id):
                        raise CheckpointUncertain(
                            "The rejection belongs to a different command ID."
                        )
                    raise CheckpointRejected(reply.error)
                if reply.command_id != request.command_id:
                    raise CheckpointUncertain("The checkpoint receipt has a different command ID.")
                if isinstance(reply, CompletedCheckpoint):
                    if reply.proof.kind is not request.kind:
                        raise CheckpointUncertain(
                            "The checkpoint proof has a different operation kind."
                        )
                    return reply.proof
                if isinstance(reply, UnknownCheckpoint):
                    raise CheckpointUncertain(reply.error.code.value)
                current = CheckpointStatus(command_id=request.command_id)
                await asyncio.sleep(0.05)
