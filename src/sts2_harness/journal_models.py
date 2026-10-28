"""Finite archival values retain material provenance without changing model rendering."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import (
    MISSING,
    BeforeValidator,
    JsonValue,
    PlainSerializer,
    SerializationInfo,
    TypeAdapter,
    ValidationInfo,
    model_serializer,
    model_validator,
)

from sts2_harness.client import models as native
from sts2_harness.config import ModelConfiguration, Protocol
from sts2_harness.context.models import (
    ActContent,
    AutomaticContent,
    AutomaticMessage,
    HandoffContent,
    ProjectedResult,
    ResultMessage,
    ReviewContent,
    RunActContent,
    RunActHandoffContent,
    ShortState,
    TaskMessage,
    Updates,
)
from sts2_harness.knowledge import (
    Document,
    DocumentView,
    GuidanceContent,
    KnowledgeLookup,
    SkillList,
    SkillSummary,
    SubjectId,
)
from sts2_harness.llm.messages import (
    AssistantMessage,
    ChatCompletionsReply,
    FailureKind,
    ModelInput,
    ModelReply,
    NativeReply,
    ResponsesReply,
    TextMessage,
    TextRole,
    ToolDefinition,
)
from sts2_harness.material_text import MaterialParts, MaterialText
from sts2_harness.results import NotesText, NotesUpdated, Outcome


class BlobKind(StrEnum):
    VALUE = "value"
    MATERIAL = "material"
    DOCUMENT = "document"
    NATIVE = "native"


@dataclass(frozen=True, slots=True)
class BlobEncoding:
    write: Callable[[BlobKind, bytes], str]


@dataclass(slots=True)
class BlobCollector:
    blocks: dict[str, tuple[BlobKind, bytes]] = field(
        default_factory=dict[str, tuple[BlobKind, bytes]]
    )

    def collect(self, kind: BlobKind, content: bytes) -> str:
        digest = sha256(content).hexdigest()
        self.blocks[digest] = (kind, content)
        return digest


@dataclass(frozen=True, slots=True)
class BlobDecoding:
    read: Callable[[str], bytes]


class MaterialReference(native.WireModel):
    material_digest: str


class DocumentReference(native.WireModel):
    document_digest: str


class NativeReference(native.WireModel):
    native_digest: str


def _encode_native(value: str, info: SerializationInfo) -> JsonValue:
    if isinstance(info.context, BlobEncoding):
        return {"native_digest": info.context.write(BlobKind.NATIVE, value.encode())}
    return value


def _decode_native(value: object, info: ValidationInfo) -> object:
    if isinstance(info.context, BlobDecoding):
        reference = NativeReference.model_validate(value)
        content = info.context.read(reference.native_digest).decode()
        return content
    return value


type NativeOutput = Annotated[
    str, BeforeValidator(_decode_native), PlainSerializer(_encode_native, return_type=JsonValue)
]


class MaterialFields(native.WireModel):
    id: str
    version: str
    body: str


class MaterialSnapshot(MaterialFields):
    @model_serializer
    def archive(self, info: SerializationInfo) -> MaterialFields | MaterialReference:
        fields = MaterialFields(id=self.id, version=self.version, body=self.body)
        if isinstance(info.context, BlobEncoding):
            return MaterialReference(
                material_digest=info.context.write(
                    BlobKind.MATERIAL, fields.model_dump_json().encode()
                )
            )
        return fields

    @model_validator(mode="before")
    @classmethod
    def expand(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(info.context, BlobDecoding) and isinstance(value, dict):
            reference = MaterialReference.model_validate(value)
            fields = MaterialFields.model_validate_json(
                info.context.read(reference.material_digest)
            )
            return {"id": fields.id, "version": fields.version, "body": fields.body}
        return value

    @classmethod
    def of(cls, text: MaterialText) -> "MaterialSnapshot":
        return cls(id=text.id, version=text.version, body=text.body)


class ResourceSnapshot(native.WireModel):
    key: str
    description: MaterialSnapshot


class DocumentFields(native.WireModel):
    id: str
    name: MaterialSnapshot
    text: MaterialSnapshot
    resources: tuple[ResourceSnapshot, ...]
    resource: str | MISSING = MISSING
    subjects: tuple[SubjectId, ...] | MISSING = MISSING


class DocumentSnapshot(DocumentFields):
    @model_serializer
    def archive(self, info: SerializationInfo) -> DocumentFields | DocumentReference:
        fields = DocumentFields(
            id=self.id,
            name=self.name,
            text=self.text,
            resources=self.resources,
            resource=self.resource,
            subjects=self.subjects,
        )
        if isinstance(info.context, BlobEncoding):
            return DocumentReference(
                document_digest=info.context.write(
                    BlobKind.DOCUMENT, fields.model_dump_json(context=info.context).encode()
                )
            )
        return fields

    @model_validator(mode="before")
    @classmethod
    def expand(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(info.context, BlobDecoding) and isinstance(value, dict):
            reference = DocumentReference.model_validate(value)
            fields = DocumentFields.model_validate_json(
                info.context.read(reference.document_digest), context=info.context
            )
            return {
                "id": fields.id,
                "name": fields.name,
                "text": fields.text,
                "resources": fields.resources,
                "resource": fields.resource,
                "subjects": fields.subjects,
            }
        return value

    @classmethod
    def of(cls, document: DocumentView) -> "DocumentSnapshot":
        return cls(
            id=document.id,
            name=MaterialSnapshot.of(document.name),
            text=MaterialSnapshot.of(document.text),
            resources=tuple(
                ResourceSnapshot(key=item.key, description=MaterialSnapshot.of(item.description))
                for item in document.resources
            ),
            resource=document.resource,
            subjects=document.subjects if isinstance(document, Document) else MISSING,
        )


class SkillSnapshot(native.WireModel):
    id: str
    name: MaterialSnapshot
    description: MaterialSnapshot

    @classmethod
    def of(cls, skill: SkillSummary) -> "SkillSnapshot":
        return cls(
            id=skill.id,
            name=MaterialSnapshot.of(skill.name),
            description=MaterialSnapshot.of(skill.description),
        )


class KnowledgeSnapshot(native.WireModel):
    definition: native.NativeDefinition
    supplements: tuple[DocumentSnapshot, ...]


class DataKind(StrEnum):
    KNOWLEDGE = "knowledge"
    DOCUMENT = "document"
    SKILLS = "skills"
    NATIVE = "native"
    NOTES = "notes"


class KnowledgeData(native.WireModel):
    kind: Literal[DataKind.KNOWLEDGE] = DataKind.KNOWLEDGE
    entries: tuple[KnowledgeSnapshot, ...]
    missing_ids: tuple[str, ...]


class DocumentData(native.WireModel):
    kind: Literal[DataKind.DOCUMENT] = DataKind.DOCUMENT
    document: DocumentSnapshot


class SkillsData(native.WireModel):
    kind: Literal[DataKind.SKILLS] = DataKind.SKILLS
    skills: tuple[SkillSnapshot, ...]


class NativeData(native.WireModel):
    kind: Literal[DataKind.NATIVE] = DataKind.NATIVE
    value: native.InspectData | native.CatalogData | native.SearchData | native.LookupData


class NotesData(native.WireModel):
    kind: Literal[DataKind.NOTES] = DataKind.NOTES
    value: NotesText | NotesUpdated


type ArchivedData = KnowledgeData | DocumentData | SkillsData | NativeData | NotesData


def archive_data(data: ProjectedResult) -> ArchivedData | MISSING:
    value = data.data
    if value is MISSING:
        return MISSING
    if isinstance(value, KnowledgeLookup):
        return KnowledgeData(
            entries=tuple(
                KnowledgeSnapshot(
                    definition=item.definition,
                    supplements=tuple(
                        DocumentSnapshot.of(document) for document in item.supplements
                    ),
                )
                for item in value.entries
            ),
            missing_ids=value.missing_ids,
        )
    if isinstance(value, DocumentView):
        return DocumentData(document=DocumentSnapshot.of(value))
    if isinstance(value, SkillList):
        return SkillsData(skills=tuple(SkillSnapshot.of(skill) for skill in value.skills))
    if isinstance(value, (NotesText, NotesUpdated)):
        return NotesData(value=value)
    return NativeData(value=value)


class TextKind(StrEnum):
    LITERAL = "literal"
    MATERIAL = "material"
    PARTS = "parts"
    GUIDANCE = "guidance"


class LiteralText(native.WireModel):
    kind: Literal[TextKind.LITERAL] = TextKind.LITERAL
    body: str


class NamedText(native.WireModel):
    kind: Literal[TextKind.MATERIAL] = TextKind.MATERIAL
    material: MaterialSnapshot


class PartsText(native.WireModel):
    kind: Literal[TextKind.PARTS] = TextKind.PARTS
    parts: tuple[LiteralText | NamedText, ...]


class GuidanceText(native.WireModel):
    kind: Literal[TextKind.GUIDANCE] = TextKind.GUIDANCE
    documents: tuple[DocumentSnapshot, ...]


type ArchivedText = LiteralText | NamedText | PartsText | GuidanceText


def archive_text(value: str | MaterialText | MaterialParts | GuidanceContent) -> ArchivedText:
    if isinstance(value, str):
        return LiteralText(body=value)
    if isinstance(value, MaterialText):
        return NamedText(material=MaterialSnapshot.of(value))
    if isinstance(value, MaterialParts):
        return PartsText(
            parts=tuple(
                LiteralText(body=part)
                if isinstance(part, str)
                else NamedText(material=MaterialSnapshot.of(part))
                for part in value.parts
            )
        )
    return GuidanceText(documents=tuple(DocumentSnapshot.of(doc) for doc in value.guidance))


class MessageKind(StrEnum):
    TEXT = "text"
    TASK = "task"
    RUN_TASK = "run_task"
    AUTOMATIC = "automatic"
    ASSISTANT = "assistant"
    RESULT = "result"


class TextSnapshot(native.WireModel):
    kind: Literal[MessageKind.TEXT] = MessageKind.TEXT
    role: TextRole
    value: ArchivedText


class TaskSnapshot(native.WireModel):
    kind: Literal[MessageKind.TASK] = MessageKind.TASK
    role: TextRole
    content: ActContent | ReviewContent


class RunTaskSnapshot(native.WireModel):
    kind: Literal[MessageKind.RUN_TASK] = MessageKind.RUN_TASK
    role: TextRole
    content: RunActContent | RunActHandoffContent | HandoffContent


class AutomaticSnapshot(native.WireModel):
    kind: Literal[MessageKind.AUTOMATIC] = MessageKind.AUTOMATIC
    role: TextRole
    content: AutomaticContent


class NativeSnapshot(native.WireModel):
    protocol: Protocol
    native_output: NativeOutput

    @property
    def native_reply(self) -> NativeReply:
        match self.protocol:
            case Protocol.RESPONSES:
                return ResponsesReply.from_json(self.native_output)
            case Protocol.CHAT_COMPLETIONS:
                return ChatCompletionsReply.from_json(self.native_output)
            case Protocol.MESSAGES:
                raise ValueError("The archived native protocol is unsupported.")

    @model_validator(mode="after")
    def validate_native(self) -> Self:
        _ = self.native_reply
        return self


class AssistantSnapshot(NativeSnapshot):
    kind: Literal[MessageKind.ASSISTANT] = MessageKind.ASSISTANT


class ResultSnapshot(native.WireModel):
    kind: Literal[MessageKind.RESULT] = MessageKind.RESULT
    call_id: str
    outcome: Outcome
    state: ShortState | None
    updates: Updates
    data: ArchivedData | MISSING = MISSING


type MessageSnapshot = (
    TextSnapshot
    | TaskSnapshot
    | RunTaskSnapshot
    | AutomaticSnapshot
    | AssistantSnapshot
    | ResultSnapshot
)
MESSAGE = TypeAdapter(MessageSnapshot)


def archive_input(input: ModelInput) -> tuple[MessageSnapshot, ...]:
    messages: list[MessageSnapshot] = []
    for message in input:
        if isinstance(message, AutomaticMessage):
            messages.append(AutomaticSnapshot(role=message.role, content=message.content))
        elif isinstance(message, TaskMessage):
            content = message.content
            if isinstance(content, (RunActContent, HandoffContent)):
                messages.append(RunTaskSnapshot(role=message.role, content=content))
            else:
                messages.append(TaskSnapshot(role=message.role, content=content))
        elif isinstance(message, ResultMessage):
            result = message.content
            messages.append(
                ResultSnapshot(
                    call_id=message.call_id,
                    outcome=result.outcome,
                    state=result.state,
                    updates=result.updates,
                    data=archive_data(result),
                )
            )
        elif isinstance(message, TextMessage):
            messages.append(TextSnapshot(role=message.role, value=archive_text(message.value)))
        elif isinstance(message, AssistantMessage):
            messages.append(
                AssistantSnapshot(
                    protocol=message.native.protocol, native_output=message.native.to_json()
                )
            )
        else:
            raise ValueError("The model result has no supported archival semantic type.")
    return tuple(messages)


class ToolSnapshot(native.WireModel):
    name: str
    description: MaterialSnapshot
    parameters: dict[str, JsonValue]

    @classmethod
    def of(cls, tool: ToolDefinition) -> "ToolSnapshot":
        return cls(
            name=tool.name,
            description=MaterialSnapshot.of(tool.description),
            parameters=TypeAdapter(dict[str, JsonValue]).validate_python(
                tool.parameters.model_json_schema(), strict=True
            ),
        )


class ToolSet(native.WireModel):
    tools: tuple[ToolSnapshot, ...]


class ConfigurationSnapshot(native.WireModel):
    protocol: Protocol
    model: str
    reasoning_effort: str
    service_tier: str
    timeout_seconds: float
    extra_retries: int

    @classmethod
    def of(cls, config: ModelConfiguration) -> "ConfigurationSnapshot":
        return cls(
            protocol=config.protocol,
            model=config.model,
            reasoning_effort=config.reasoning_effort,
            service_tier=config.service_tier,
            timeout_seconds=config.timeout_seconds,
            extra_retries=config.extra_retries,
        )


class ReplySnapshot(NativeSnapshot):
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    requested_model: str
    requested_effort: str
    response_model: str | None
    response_service_tier: str | None

    @classmethod
    def of(cls, reply: ModelReply) -> "ReplySnapshot":
        return cls(
            protocol=reply.assistant.native.protocol,
            native_output=reply.assistant.native.to_json(),
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            cache_read_tokens=reply.usage.cache_read_tokens,
            cache_write_tokens=reply.usage.cache_write_tokens,
            requested_model=reply.metadata.requested_model,
            requested_effort=reply.metadata.requested_effort,
            response_model=reply.metadata.response_model,
            response_service_tier=reply.metadata.response_service_tier,
        )


class ModelAttempt(native.WireModel):
    index: int
    protocol_encoding_digest: str | None = None
    reply: ReplySnapshot | None = None
    failure: FailureKind | None = None


class InputDelta(native.WireModel):
    base_sequence: int | None
    prefix_length: int
    suffix: tuple[str, ...]
    semantic_digest: str
    tools: str
    configuration: str


class ModelRecord(native.WireModel):
    sequence: int
    confirmed_operation_cursor: int
    role: native.DecisionOwner
    stream: str
    position: int
    base_sequence: int | None
    prefix_length: int
    suffix_length: int
    semantic_digest: str
    input: tuple[MessageSnapshot, ...]
    tools: tuple[ToolSnapshot, ...]
    configuration: ConfigurationSnapshot
    attempts: tuple[ModelAttempt, ...]
    delivered: bool
    failure: FailureKind | None


class MaterialKind(StrEnum):
    PROMPT = "prompt"
    LIST = "list"
    READ = "read"
    LOOKUP = "lookup"


class MaterialFailure(StrEnum):
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class PromptRead(native.WireModel):
    kind: Literal[MaterialKind.PROMPT] = MaterialKind.PROMPT
    name: str


class SkillListing(native.WireModel):
    kind: Literal[MaterialKind.LIST] = MaterialKind.LIST
    role: native.DecisionOwner


class DocumentRead(native.WireModel):
    kind: Literal[MaterialKind.READ] = MaterialKind.READ
    id: str
    resource: str | None


class DocumentLookup(native.WireModel):
    kind: Literal[MaterialKind.LOOKUP] = MaterialKind.LOOKUP
    subjects: tuple[SubjectId, ...]


type MaterialRequest = PromptRead | SkillListing | DocumentRead | DocumentLookup
MATERIAL_REQUEST = TypeAdapter(MaterialRequest)


class MaterialStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class PromptReadResult(native.WireModel):
    status: Literal[MaterialStatus.COMPLETED] = MaterialStatus.COMPLETED
    kind: Literal[MaterialKind.PROMPT] = MaterialKind.PROMPT
    prompt: MaterialSnapshot


class SkillListingResult(native.WireModel):
    status: Literal[MaterialStatus.COMPLETED] = MaterialStatus.COMPLETED
    kind: Literal[MaterialKind.LIST] = MaterialKind.LIST
    skills: tuple[SkillSnapshot, ...]


class DocumentReadResult(native.WireModel):
    status: Literal[MaterialStatus.COMPLETED] = MaterialStatus.COMPLETED
    kind: Literal[MaterialKind.READ] = MaterialKind.READ
    document: DocumentSnapshot


class DocumentLookupResult(native.WireModel):
    status: Literal[MaterialStatus.COMPLETED] = MaterialStatus.COMPLETED
    kind: Literal[MaterialKind.LOOKUP] = MaterialKind.LOOKUP
    documents: tuple[DocumentSnapshot, ...]


class MaterialFailed(native.WireModel):
    status: Literal[MaterialStatus.FAILED] = MaterialStatus.FAILED
    failure: MaterialFailure
    message: str


type MaterialReply = (
    PromptReadResult
    | SkillListingResult
    | DocumentReadResult
    | DocumentLookupResult
    | MaterialFailed
)
MATERIAL_REPLY = TypeAdapter(MaterialReply)


class MaterialRecord(native.WireModel):
    sequence: int
    confirmed_operation_cursor: int
    request: MaterialRequest
    reply: MaterialReply | None
