"""Framework-independent Assistant HTTP input and neutral stream contracts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CHAT_ATTACHMENT_CHAR_LIMIT = 200_000
CHAT_ATTACHMENTS_TOTAL_CHAR_LIMIT = 320_000
MAX_CHAT_PROPOSAL_OPERATIONS = 200
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
OutputLocale = Literal["zh-CN", "en"]
DEFAULT_OUTPUT_LOCALE: OutputLocale = "zh-CN"
TrustedOperationType = Literal[
    "application_update",
    "review_timeline_entry_edit",
    "review_record",
    "preference_update",
]
ProposalSurface = Literal[
    "intake",
    "application_merge",
    "application_delete",
    "review_undo",
]
UiActionKind = Literal[
    "open_application",
    "open_timeline",
    "open_application_research",
    "open_resume_adaptation",
    "open_grill_session",
    "open_grill",
    "open_questions",
    "open_library",
    "open_resume",
]


class ChatUiActionReference(BaseModel):
    """Server-authored navigation only; labels and URLs stay in the frontend allowlist."""

    model_config = ConfigDict(extra="forbid")

    kind: UiActionKind
    resource_id: Annotated[int, Field(strict=True, gt=0)] | None = None

    @model_validator(mode="after")
    def resource_matches_kind(self):
        requires_resource = self.kind not in {
            "open_timeline", "open_grill", "open_questions", "open_library",
        }
        if requires_resource != (self.resource_id is not None):
            raise ValueError("ui action resource_id does not match kind")
        return self


class ChatAttachment(BaseModel):
    """Accept only upload-produced attachment shapes with bounded client echoes."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["document", "image"]
    filename: str = Field(min_length=1, max_length=255)
    stored: str | None = Field(default=None, max_length=255)
    text: str | None = Field(default=None, max_length=CHAT_ATTACHMENT_CHAR_LIMIT)
    truncated: bool = False

    @model_validator(mode="after")
    def required_payload(self):
        if self.kind == "image" and not self.stored:
            raise ValueError("图片附件缺少受管文件标识")
        if self.kind == "image" and self.stored and Path(self.stored).name != self.stored:
            raise ValueError("图片附件标识无效")
        if self.kind == "image" and self.text is not None:
            raise ValueError("图片附件不能携带文档文本")
        if self.kind == "document" and not self.text:
            raise ValueError("文档附件缺少已提取文本")
        if self.kind == "document" and self.stored is not None:
            raise ValueError("文档附件不能引用临时图片文件")
        return self


class ChatRequest(BaseModel):
    """Logical request with pre-send session/turn IDs for exact response-loss retry."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=50_000)
    session_id: UUID
    client_turn_id: UUID
    output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE
    review_supplement_reference: UUID | None = None
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=8)

    @field_validator(
        "session_id",
        "client_turn_id",
        "review_supplement_reference",
        mode="before",
    )
    @classmethod
    def canonical_uuid(cls, value):
        """Reject UUIDs Pydantic would rewrite, preserving byte-stable identity."""
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise ValueError("必须是规范 UUID 字符串")
        try:
            canonical = str(UUID(value))
        except (ValueError, AttributeError) as error:
            raise ValueError("必须是规范 UUID 字符串") from error
        if value != canonical:
            raise ValueError("必须使用小写、带连字符的规范 UUID")
        return value

    @model_validator(mode="after")
    def total_document_budget(self):
        total = sum(len(item.text or "") for item in self.attachments if item.kind == "document")
        if total > CHAT_ATTACHMENTS_TOTAL_CHAR_LIMIT:
            raise ValueError(f"文档附件合计最多 {CHAT_ATTACHMENTS_TOTAL_CHAR_LIMIT} 个字符")
        return self


class ChatProposalOperationReference(BaseModel):
    """Confirmation proposal recoverable from the turn ledger without body text."""

    model_config = ConfigDict(extra="forbid")

    surface: ProposalSurface
    operation_id: UUID

    @field_validator("operation_id", mode="before")
    @classmethod
    def canonical_operation_id(cls, value):
        if isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise ValueError("operation_id 必须是规范 UUID 字符串")
        try:
            canonical = str(UUID(value))
        except (ValueError, AttributeError) as error:
            raise ValueError("operation_id 必须是规范 UUID 字符串") from error
        if value != canonical:
            raise ValueError("operation_id 必须使用小写、带连字符的规范 UUID")
        return value


class ChatTurnStatusResponse(BaseModel):
    """Durable agent-turn state exposing only the recovery UI projection."""

    model_config = ConfigDict(extra="forbid")

    client_turn_id: UUID
    state: Literal["absent", "running", "completed", "unknown", "cancelled"]
    terminal: bool
    proposal_operations: list[ChatProposalOperationReference] = Field(
        max_length=MAX_CHAT_PROPOSAL_OPERATIONS,
    )

    @model_validator(mode="after")
    def terminal_matches_state(self):
        if self.terminal != (self.state in {"completed", "unknown", "cancelled"}):
            raise ValueError("terminal 必须且只能对应 completed/unknown/cancelled")
        identities = {
            (item.surface, item.operation_id)
            for item in self.proposal_operations
        }
        if len(identities) != len(self.proposal_operations):
            raise ValueError("proposal_operations 不能包含重复引用")
        if self.state in {"absent", "cancelled"} and self.proposal_operations:
            raise ValueError("absent/cancelled turn 不能包含 proposal_operations")
        return self


class ChatTurnCancelRequest(BaseModel):
    """Explicit empty command that rejects fields mistaken for future parameters."""

    model_config = ConfigDict(extra="forbid")


class ChatRecoveryScopeResponse(BaseModel):
    """Minimal opaque recovery namespace for the authenticated identity."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class ChatImageUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    kind: Literal["image"]
    filename: str
    stored: str


class ChatDocumentUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    kind: Literal["document"]
    filename: str
    text: str
    truncated: bool = False


class ChatUploadErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["error"]
    message: str


ChatUploadResponse = (
    ChatImageUploadResponse | ChatDocumentUploadResponse | ChatUploadErrorResponse
)


class ChatUploadDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


@dataclass(frozen=True, slots=True)
class ChatStreamEvent:
    """Transport-neutral orchestration event adapted to SSE by the API."""

    event: Literal[
        "message_delta", "message_snapshot", "tool_status", "ui_action", "done", "error",
    ]
    data: dict[str, Any]
