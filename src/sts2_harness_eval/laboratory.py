"""One-shot development laboratory IO, separate from the gameplay core."""

from enum import StrEnum
from types import TracebackType
from typing import Self

import httpx2 as httpx

from sts2_harness.client import models as native


class LaboratoryRecipe(native.WireModel):
    deck: tuple[native.Nonempty, ...]
    relics: tuple[native.Nonempty, ...]
    potions: tuple[native.Nonempty, ...]
    encounter: native.Nonempty


class LaboratoryOutcome(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class LaboratoryErrorCode(StrEnum):
    INVALID_RECIPE = "invalid_recipe"
    UNSUPPORTED = "unsupported"
    NATIVE_BUSY = "native_busy"
    DEVELOPMENT_DISABLED = "development_disabled"
    NATIVE_CONFLICT = "native_conflict"
    NATIVE_INCOMPLETE = "native_incomplete"


class LaboratoryError(native.WireModel):
    code: LaboratoryErrorCode
    message: native.Nonempty


class LaboratoryReply(native.WireModel):
    status: LaboratoryOutcome
    error: LaboratoryError | None
    prepared_observation: native.Observation | None
    observation: native.Observation | None


class LaboratoryClient:
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

    async def construct(self, recipe: LaboratoryRecipe) -> LaboratoryReply:
        response = await self._http.post(
            "/operator/eval/scenario",
            content=recipe.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return LaboratoryReply.model_validate_json(response.content)
