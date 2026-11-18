"""Single requests to the Mod, without retries or retained game state."""

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
    ControlFailed,
    ControlIntent,
    ControlReply,
    ControlRequest,
    DetailData,
    DisplayReply,
    DisplayRequest,
    ErrorCode,
    HistoryData,
    InspectArea,
    InspectCompleted,
    InspectQuery,
    InspectReply,
    InspectRequest,
    InvalidRequestReply,
    LookupData,
    LookupQuery,
    MapData,
    PresentReply,
    PresentRequest,
    ResultStatus,
    SearchData,
    StateReply,
    Status,
    StepReceipt,
    StepRequest,
    TechnicalError,
    VerifyPresentationReply,
    VerifyPresentationRequest,
    WireModel,
)

type ActionResult = StepReceipt | InvalidRequestReply | TechnicalError
type PresentResult = PresentReply | InvalidRequestReply | TechnicalError
type VerifyPresentationResult = VerifyPresentationReply | InvalidRequestReply | TechnicalError
type ControlResult = ControlReply | ControlFailed | InvalidRequestReply | TechnicalError
type DisplayResult = DisplayReply | InvalidRequestReply | TechnicalError

type StatusResult = Status | InvalidRequestReply | TechnicalError
type StateResult = StateReply | InvalidRequestReply | TechnicalError
type InspectResult = InspectReply | InvalidRequestReply | TechnicalError
type CatalogResult = CatalogReply | InvalidRequestReply | TechnicalError
type QueryResult = StateResult | InspectResult

_ACTION = TypeAdapter(StepReceipt)
_PRESENT = TypeAdapter(PresentReply)
_VERIFY_PRESENTATION = TypeAdapter(VerifyPresentationReply)
_CONTROL = TypeAdapter(ControlReply | ControlFailed)
_DISPLAY = TypeAdapter(DisplayReply)
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
        super().__init__(f"Mod request unavailable: {reason.value}.")


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

    async def submit(self, request: StepRequest) -> ActionResult:
        return await self._request("POST", "/action", _ACTION, request)

    async def present(self, request: PresentRequest) -> PresentResult:
        return await self._request("POST", "/map/present", _PRESENT, request)

    async def verify_presentation(
        self, request: VerifyPresentationRequest
    ) -> VerifyPresentationResult:
        return await self._request("POST", "/map/restore/verify", _VERIFY_PRESENTATION, request)

    async def control(self, mode: ControlIntent) -> ControlResult:
        return await self._request("POST", "/control", _CONTROL, ControlRequest(mode=mode))

    async def display(self, reason: str, *, timeout_seconds: float = 1.0) -> DisplayResult:
        return await self._request(
            "POST",
            "/display",
            _DISPLAY,
            DisplayRequest(reason=reason),
            timeout_seconds=timeout_seconds,
        )

    async def status(self) -> StatusResult:
        return await self._request("GET", "/status", _STATUS)

    async def state(self) -> StateResult:
        return await self._request("GET", "/state", _STATE)

    async def inspect(self, query: InspectQuery) -> InspectResult:
        result = await self._request("POST", "/inspect", _INSPECT, InspectRequest(query=query))
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
            result = await self._request("POST", "/catalog", _CATALOG, request)
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
        if isinstance(request, LookupQuery) and isinstance(result.data, LookupData):
            found_ids = tuple(entry.definition_id for entry in result.data.entries)
            missing_ids = result.data.missing_ids
            accounted_ids = found_ids + missing_ids
            ids_complete = len(accounted_ids) == len(set(accounted_ids)) and set(
                accounted_ids
            ) == set(request.ids)
            ids_ordered = found_ids == tuple(
                definition_id for definition_id in request.ids if definition_id in found_ids
            ) and missing_ids == tuple(
                definition_id for definition_id in request.ids if definition_id in missing_ids
            )
            native_ids_match = all(
                entry.definition_id == entry.native.definition_id for entry in result.data.entries
            )
            if not ids_complete or not ids_ordered or not native_ids_match:
                raise ClientUnavailable(ClientFailureKind.PROTOCOL)
        return result

    async def _request[T](
        self,
        method: str,
        path: str,
        adapter: TypeAdapter[T],
        request: WireModel | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> T | InvalidRequestReply | TechnicalError:
        try:
            response = await self._http.request(
                method,
                path,
                content=request.to_json().encode("utf-8") if request else None,
                headers={"content-type": "application/json"} if request else None,
                timeout=httpx.USE_CLIENT_DEFAULT if timeout_seconds is None else timeout_seconds,
            )
        except httpx.TimeoutException:
            raise ClientUnavailable(ClientFailureKind.TIMEOUT) from None
        except httpx.TransportError:
            raise ClientUnavailable(ClientFailureKind.CONNECTION) from None
        except httpx.DecodingError:
            raise ClientUnavailable(ClientFailureKind.PROTOCOL) from None
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
