"""Single read requests to the Mod, without retries or retained game state."""

import json
from enum import StrEnum
from types import TracebackType
from typing import Self

import httpx2 as httpx
from pydantic import MISSING, TypeAdapter, ValidationError

from sts2_harness.client.models import (
    CardQuery,
    CatalogCompleted,
    CatalogData,
    CatalogFailed,
    CatalogFailure,
    CatalogMode,
    CatalogReply,
    CatalogRequest,
    DetailData,
    ErrorCode,
    HistoryData,
    InspectArea,
    InspectCompleted,
    InspectQuery,
    InspectReply,
    InspectRequest,
    InvalidRequestReply,
    LookupData,
    MapData,
    ResultStatus,
    SearchData,
    StateReply,
    Status,
    TechnicalError,
    WireModel,
)

type StatusResult = Status | InvalidRequestReply | TechnicalError
type StateResult = StateReply | InvalidRequestReply | TechnicalError
type InspectResult = InspectReply | InvalidRequestReply | TechnicalError
type CatalogResult = CatalogReply | InvalidRequestReply | TechnicalError
type QueryResult = StateResult | InspectResult

_STATUS = TypeAdapter(Status)
_STATE = TypeAdapter(StateReply)
_INSPECT = TypeAdapter(InspectReply)
_CATALOG = TypeAdapter(CatalogReply)
_INVALID = TypeAdapter(InvalidRequestReply)
_TECHNICAL = TypeAdapter(TechnicalError)


class ClientFailureKind(StrEnum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    HTTP = "http"


class ClientUnavailable(Exception):
    """A boundary failure with no untrusted response body in its message."""

    def __init__(self, reason: ClientFailureKind) -> None:
        self.reason = reason
        super().__init__(f"Mod read unavailable: {reason.value}.")


class _UnknownNativeCategory(Exception):
    pass


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for name, value in pairs:
        if name in fields:
            raise ValueError("Duplicate JSON keys are not permitted.")
        fields[name] = value
    return fields


class ModClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout_seconds,
            trust_env=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def status(self) -> StatusResult:
        return await self._read("GET", "/status", _STATUS)

    async def state(self) -> StateResult:
        return await self._read("GET", "/state", _STATE)

    async def inspect(self, query: InspectQuery) -> InspectResult:
        result = await self._read("POST", "/inspect", _INSPECT, InspectRequest(query=query))
        if not isinstance(result, InspectCompleted):
            return result
        expected = (
            MapData
            if query.area is InspectArea.MAP
            else HistoryData
            if query.area is InspectArea.COMBAT_HISTORY
            else DetailData
        )
        if type(result.data) is not expected:
            raise ClientUnavailable(ClientFailureKind.PROTOCOL)
        if isinstance(result.data, DetailData):
            requested_upgrade = isinstance(query, CardQuery) and query.upgrade is True
            if requested_upgrade != (result.data.upgrades is not MISSING):
                raise ClientUnavailable(ClientFailureKind.PROTOCOL)
        return result

    async def catalog(self, request: CatalogRequest) -> CatalogResult:
        try:
            result = await self._read("POST", "/catalog", _CATALOG, request)
        except _UnknownNativeCategory:
            return CatalogFailed(
                status=ResultStatus.FAILED,
                error=CatalogFailure(
                    code=ErrorCode.CATALOG_UNAVAILABLE,
                    message="The native catalog contains an unsupported definition category.",
                ),
            )
        if not isinstance(result, CatalogCompleted):
            return result
        expected = {
            CatalogMode.CATALOG: CatalogData,
            CatalogMode.SEARCH: SearchData,
            CatalogMode.LOOKUP: LookupData,
        }[request.mode]
        if type(result.data) is not expected:
            raise ClientUnavailable(ClientFailureKind.PROTOCOL)
        return result

    async def _read[T](
        self, method: str, path: str, adapter: TypeAdapter[T], request: WireModel | None = None
    ) -> T | InvalidRequestReply | TechnicalError:
        try:
            response = await self._http.request(
                method,
                path,
                content=request.to_json().encode("utf-8") if request else None,
                headers={"content-type": "application/json"} if request else None,
            )
        except httpx.TimeoutException:
            raise ClientUnavailable(ClientFailureKind.TIMEOUT) from None
        except httpx.TransportError:
            raise ClientUnavailable(ClientFailureKind.CONNECTION) from None
        if response.status_code not in {200, 400, 500}:
            raise ClientUnavailable(ClientFailureKind.HTTP)
        try:
            payload = response.content.decode("utf-8")
            # Pydantic accepts repeated keys; validate uniqueness before typed decoding.
            json.loads(payload, object_pairs_hook=_unique_keys)
            if response.status_code == 400:
                return _INVALID.validate_json(payload, strict=True)
            if response.status_code == 500:
                return _TECHNICAL.validate_json(payload, strict=True)
            return adapter.validate_json(payload, strict=True)
        except ValidationError as error:
            if path == "/catalog" and any(
                detail["type"] == "union_tag_invalid"
                and len(detail["loc"]) >= 2
                and detail["loc"][-2] == "entries"
                for detail in error.errors(include_input=False)
            ):
                raise _UnknownNativeCategory from None
            raise ClientUnavailable(ClientFailureKind.PROTOCOL) from None
        except (UnicodeError, ValueError):
            raise ClientUnavailable(ClientFailureKind.PROTOCOL) from None
