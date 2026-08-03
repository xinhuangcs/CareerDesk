"""Stable operation contracts for ``record_review`` intake and supplements.

The model-visible command never receives operation or turn identities.  These
contracts are persisted by the trusted workflow and revalidated on every read;
database JSON is therefore treated as untrusted input rather than as a DTO.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ....platform.database import normalize_application_identity_part, squash_whitespace
from ...applications.public import ApplicationNextAction, ApplicationStage
from ..ai_models import (
    MAX_REVIEW_BATCH_ITEMS,
    MAX_REVIEW_BATCH_TOTAL_TEXT_CHARS,
    ReviewExtraction,
)

REVIEW_RECORD_CONTRACT_VERSION = 1
MAX_REVIEW_RECORD_SOURCE_CHARS = 50_000
MAX_REVIEW_RECORD_COMBINED_CHARS = 100_000
MAX_REVIEW_RECORD_SUPPLEMENTS = 20
# Only the hard application identity can require clarification, and it has
# exactly two independent fields. Keep this aligned with the browser contract.
MAX_REVIEW_RECORD_MISSING_FIELDS = 2
MAX_REVIEW_RECORD_ASK_CHARS = 8_000
MAX_REVIEW_RECORD_ERROR_CHARS = 500
MAX_REVIEW_RECORD_OPERATIONS_PER_TURN = MAX_REVIEW_BATCH_ITEMS
MAX_REVIEW_RECORD_BATCH_DECISION_CHARS = MAX_REVIEW_BATCH_TOTAL_TEXT_CHARS
MAX_PENDING_REVIEW_RECORD_CLARIFICATIONS = 100
MAX_PENDING_REVIEW_RECORD_CONFIRMATIONS = 100

UUIDText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=False,
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    ),
]
DigestText = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
TimestampText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
ISODateText = Annotated[
    str,
    StringConstraints(min_length=10, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$"),
]
AskText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REVIEW_RECORD_ASK_CHARS),
]
ErrorText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REVIEW_RECORD_ERROR_CHARS),
]


def _canonical_uuid(value: str) -> str:
    try:
        canonical = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("value must be a UUID") from error
    if canonical != value:
        raise ValueError("value must be a canonical lowercase UUID")
    return value


def _real_iso_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("date must be a real ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError("date must be a canonical ISO date")
    return value


class ReviewRecordMissingField(BaseModel):
    """One deterministic clarification requested by local policy."""

    model_config = ConfigDict(extra="forbid", strict=True)

    field: Literal["company", "position"]
    ask: AskText


def _require_unique_missing_fields(
    values: list[ReviewRecordMissingField],
) -> list[ReviewRecordMissingField]:
    fields = [item.field for item in values]
    if len(fields) != len(set(fields)):
        raise ValueError("missing identity fields must be unique")
    return values


class ReviewRecordDecision(BaseModel):
    """One exact child decision in a user-confirmed Review batch."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: UUIDText
    action: Literal["approve", "reject"]
    edited_extraction: ReviewExtraction | None = None

    @field_validator("operation_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        return _canonical_uuid(value)

    @field_validator("edited_extraction", mode="before")
    @classmethod
    def validate_strict_edited_extraction(cls, value):
        """Browser edits must not inherit the LLM-facing model's coercions."""
        if value is None or isinstance(value, ReviewExtraction):
            return value
        return ReviewExtraction.model_validate(value, strict=True)

    @model_validator(mode="after")
    def validate_edit_action(self) -> "ReviewRecordDecision":
        if self.action == "reject" and self.edited_extraction is not None:
            raise ValueError("rejected Review child cannot carry an edited extraction")
        return self


class ReviewRecordProposal(BaseModel):
    """Immutable phase-one claim persisted before the external extractor runs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["review_record"]
    contract_version: Literal[REVIEW_RECORD_CONTRACT_VERSION]
    operation_id: UUIDText
    review_reference: UUIDText
    client_turn_id: UUIDText
    request_digest: DigestText
    source_digest: DigestText
    combined_digest: DigestText
    attempt_token: UUIDText
    mode: Literal["initial", "supplement"]
    effective_date: ISODateText
    source_journal_id: int = Field(gt=0)
    target_journal_id: int = Field(gt=0)
    target_expected_state: Literal["pending", "awaiting_user"]
    target_expected_revision: int = Field(ge=0)

    @field_validator(
        "operation_id",
        "review_reference",
        "client_turn_id",
        "attempt_token",
    )
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        return _canonical_uuid(value)

    @field_validator("effective_date")
    @classmethod
    def validate_effective_date(cls, value: str) -> str:
        return _real_iso_date(value)

    @model_validator(mode="after")
    def validate_mode_shape(self) -> "ReviewRecordProposal":
        if self.mode == "initial":
            if self.review_reference != self.operation_id:
                raise ValueError("initial review_reference must equal operation_id")
            if self.source_journal_id != self.target_journal_id:
                raise ValueError("initial source must be the target review")
            if self.target_expected_state != "pending" or self.target_expected_revision != 0:
                raise ValueError("initial target must start at pending revision zero")
        else:
            if self.source_journal_id == self.target_journal_id:
                raise ValueError("supplement source must be separate from its review")
            if self.target_expected_state not in {"pending", "awaiting_user"}:
                raise ValueError("supplement target must remain pending or awaiting_user")
        return self


class ReviewRecordDerivation(BaseModel):
    """Exact public shape returned by the existing Review derivation primitive."""

    model_config = ConfigDict(extra="forbid", strict=True)

    application_id: int = Field(gt=0)
    application_created: bool = False
    timeline_entry_ids: list[int] = Field(min_length=1, max_length=1)
    question_ids: list[int] = Field(default_factory=list, max_length=50)
    knowledge_point_ids: list[int] = Field(default_factory=list, max_length=150)
    status_log_ids: list[int] = Field(default_factory=list, max_length=1)
    application_before: "ReviewRecordProjectionSnapshot"
    application_after: "ReviewRecordProjectionSnapshot"
    revision: int = Field(gt=0)

    @field_validator(
        "timeline_entry_ids",
        "question_ids",
        "knowledge_point_ids",
        "status_log_ids",
    )
    @classmethod
    def positive_unique_ids(cls, values: list[int]) -> list[int]:
        if any(isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("derivation ids must be positive integers")
        if len(values) != len(set(values)):
            raise ValueError("derivation ids must be unique")
        return values

    @field_validator("knowledge_point_ids")
    @classmethod
    def sorted_knowledge_ids(cls, values: list[int]) -> list[int]:
        if values != sorted(values):
            raise ValueError("knowledge point ids must be sorted")
        return values


class ReviewRecordProjectionSnapshot(BaseModel):
    """Application projection needed for exact conditional Review undo."""

    model_config = ConfigDict(extra="forbid", strict=True)

    stage: ApplicationStage
    current_step: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
    ] | None = None
    current_state_entry_id: int | None = Field(default=None, gt=0)
    next_action: ApplicationNextAction | None = None
    paused_from_stage: ApplicationStage | None = None
    pause_reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000),
    ] | None = None
    channel: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ] | None = None
    applied_date: ISODateText | None = None
    revision: int = Field(ge=0)

    @field_validator("applied_date")
    @classmethod
    def validate_applied_date(cls, value: str | None) -> str | None:
        return None if value is None else _real_iso_date(value)

    @model_validator(mode="after")
    def validate_application_state(self) -> "ReviewRecordProjectionSnapshot":
        if self.stage in {"withdrawn", "rejected"} and self.next_action is not None:
            raise ValueError("closed application snapshot cannot retain a next action")
        if self.stage != "pooled":
            if self.paused_from_stage is not None or self.pause_reason is not None:
                raise ValueError("non-pooled snapshot cannot retain pause metadata")
        elif self.paused_from_stage not in {
            None, "backlog", "applied", "written_test", "interviewing", "offer",
        }:
            raise ValueError("pooled snapshot paused_from_stage must be active")
        return self

class ReviewRecordApplication(BaseModel):
    """The actual application identity selected by deterministic derivation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    company: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    position: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


def _expected_projected_state(
    current_stage: ApplicationStage,
    current_step: str | None,
    extraction: ReviewExtraction,
) -> tuple[ApplicationStage, str | None]:
    """Recompute the exact current projection shown in the confirmation card."""
    projected = extraction.projected_state
    if projected is None:
        return current_stage, current_step
    return projected.stage or current_stage, projected.current_step or current_step


def _expected_projected_next_action(
    current: ApplicationNextAction | None,
    projected_stage: ApplicationStage,
    extraction: ReviewExtraction,
) -> ApplicationNextAction | None:
    """Terminal stages clear plans; otherwise an explicit plan replaces the old one."""
    if projected_stage in {"withdrawn", "rejected"}:
        return None
    if extraction.clear_next_action:
        return None
    return extraction.next_action or current


class ReviewRecordExistingTargetPlan(BaseModel):
    """Frozen identity and state effect for one existing application."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["existing"]
    application_id: int = Field(gt=0)
    company: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    position: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    created_time: TimestampText
    revision: int = Field(ge=0)
    current_stage: ApplicationStage
    current_step: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
    ] | None
    projected_stage: ApplicationStage
    projected_step: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
    ] | None
    current_next_action: ApplicationNextAction | None
    projected_next_action: ApplicationNextAction | None
    current_applied_date: ISODateText | None
    projected_applied_date: ISODateText | None
    current_channel: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None
    projected_channel: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None

    @field_validator(
        "current_applied_date",
        "projected_applied_date",
    )
    @classmethod
    def validate_dates(cls, value: str | None) -> str | None:
        return None if value is None else _real_iso_date(value)


class ReviewRecordNewTargetPlan(BaseModel):
    """Frozen identity and state effect for one application that would be created."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["new"]
    company: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    position: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    current_stage: Literal["backlog"]
    current_step: None
    projected_stage: ApplicationStage
    projected_step: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
    ] | None
    current_next_action: None
    projected_next_action: ApplicationNextAction | None
    current_applied_date: None
    projected_applied_date: ISODateText | None
    current_channel: None
    projected_channel: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None

    @field_validator("projected_applied_date")
    @classmethod
    def validate_dates(cls, value: str | None) -> str | None:
        return None if value is None else _real_iso_date(value)


ReviewRecordTargetPlan = ReviewRecordExistingTargetPlan | ReviewRecordNewTargetPlan


class ReviewRecordResult(BaseModel):
    """Terminal success, including clarification as a completed command outcome."""

    model_config = ConfigDict(extra="forbid", strict=True)

    outcome: Literal["applied", "needs_clarification"]
    review_reference: UUIDText
    source_journal_id: int = Field(gt=0)
    target_journal_id: int = Field(gt=0)
    target_revision: int = Field(gt=0)
    extraction: ReviewExtraction
    missing: list[ReviewRecordMissingField] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_RECORD_MISSING_FIELDS,
    )
    derivation: ReviewRecordDerivation | None = None
    application: ReviewRecordApplication | None = None

    @field_validator("review_reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        return _canonical_uuid(value)

    _validate_unique_missing_fields = field_validator("missing")(
        _require_unique_missing_fields
    )

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> "ReviewRecordResult":
        if self.outcome == "applied":
            if self.missing or self.derivation is None or self.application is None:
                raise ValueError("applied result requires derivation/application and no missing fields")
            if self.derivation.application_id != self.application.id:
                raise ValueError("derivation and application identities must agree")
            if self.derivation.revision != self.target_revision:
                raise ValueError("derivation and target revisions must agree")
            extraction = self.extraction
            before = self.derivation.application_before
            after = self.derivation.application_after
            expected_stage, expected_step = _expected_projected_state(
                before.stage,
                before.current_step,
                extraction,
            )
            expected_next = _expected_projected_next_action(
                before.next_action,
                expected_stage,
                extraction,
            )
            expected_applied_date = before.applied_date
            if (
                before.stage != "applied"
                and extraction.projected_state is not None
                and extraction.projected_state.stage == "applied"
                and expected_applied_date is None
            ):
                expected_applied_date = (
                    extraction.history.date if extraction.history is not None else None
                )
            expected_channel = extraction.channel or before.channel
            state_changed = (
                expected_stage,
                expected_step,
            ) != (before.stage, before.current_step)
            expected_state_entry_id = (
                self.derivation.timeline_entry_ids[0]
                if state_changed else before.current_state_entry_id
            )
            if expected_stage == "pooled":
                expected_paused_from = (
                    before.stage
                    if before.stage in {
                        "backlog", "applied", "written_test", "interviewing", "offer",
                    }
                    else before.paused_from_stage
                )
                expected_pause_reason = before.pause_reason if before.stage == "pooled" else None
            else:
                expected_paused_from = None
                expected_pause_reason = None
            if (
                after.stage != expected_stage
                or after.current_step != expected_step
                or after.current_state_entry_id != expected_state_entry_id
                or after.next_action != expected_next
                or after.paused_from_stage != expected_paused_from
                or after.pause_reason != expected_pause_reason
                or after.channel != expected_channel
                or after.applied_date != expected_applied_date
                or after.revision != before.revision + 1
            ):
                raise ValueError("derivation application projections disagree with extraction")
            if normalize_application_identity_part(extraction.company) != (
                normalize_application_identity_part(self.application.company)
            ):
                raise ValueError("result company identity disagrees with extraction")
            if extraction.position is not None and (
                normalize_application_identity_part(extraction.position)
                != normalize_application_identity_part(self.application.position)
            ):
                raise ValueError("result position identity disagrees with extraction")
            if self.derivation.application_created and before != ReviewRecordProjectionSnapshot(
                stage="backlog",
                current_step=None,
                current_state_entry_id=None,
                next_action=None,
                paused_from_stage=None,
                pause_reason=None,
                channel=None,
                applied_date=None,
                revision=0,
            ):
                raise ValueError("created application before snapshot is not empty")
        elif not self.missing or self.derivation is not None or self.application is not None:
            raise ValueError("clarification result requires missing fields only")
        return self


class ReviewRecordPreview(BaseModel):
    """Safe extracted facts persisted while waiting for an explicit page decision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    extraction: ReviewExtraction
    target_plan: ReviewRecordTargetPlan | None
    missing: list[ReviewRecordMissingField] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_RECORD_MISSING_FIELDS,
    )

    _validate_unique_missing_fields = field_validator("missing")(
        _require_unique_missing_fields
    )

    @model_validator(mode="after")
    def validate_target_shape(self) -> "ReviewRecordPreview":
        if self.target_plan is None:
            if not self.missing or any(
                item.field not in {"company", "position"} for item in self.missing
            ):
                raise ValueError("missing target plan requires hard identity clarification")
            return self
        if self.missing:
            raise ValueError("resolved target plan cannot retain missing fields")
        if self.extraction.clear_next_action and self.target_plan.current_next_action is None:
            raise ValueError("cannot clear a next action that does not exist")
        # Existing targets use stored text; extraction may differ only in whitespace.
        if squash_whitespace(self.extraction.company) != squash_whitespace(
            self.target_plan.company
        ):
            raise ValueError("target plan company must match extracted company")
        if self.target_plan.kind == "new" and squash_whitespace(
            self.extraction.position
        ) != squash_whitespace(self.target_plan.position):
            raise ValueError("new target position must match extracted position")
        expected_stage, expected_step = _expected_projected_state(
            self.target_plan.current_stage,
            self.target_plan.current_step,
            self.extraction,
        )
        if (
            self.target_plan.projected_stage,
            self.target_plan.projected_step,
        ) != (expected_stage, expected_step):
            raise ValueError("target plan projected state must match extracted effect")
        if expected_stage in {"withdrawn", "rejected"}:
            if self.extraction.next_action is not None:
                raise ValueError("closed application cannot retain an extracted next action")
            if (
                self.target_plan.current_next_action is not None
                and not self.extraction.clear_next_action
            ):
                raise ValueError("closing an application must explicitly clear its next action")
        if (
            self.extraction.history is None
            and not self.extraction.clear_next_action
            and self.extraction.next_action is None
            and (expected_stage, expected_step)
            == (self.target_plan.current_stage, self.target_plan.current_step)
        ):
            raise ValueError("review preview must contain meaningful progress")
        expected_next = _expected_projected_next_action(
            self.target_plan.current_next_action,
            expected_stage,
            self.extraction,
        )
        if self.target_plan.projected_next_action != expected_next:
            raise ValueError("target plan projected next action must match extracted effect")
        expected_applied_date = self.target_plan.current_applied_date
        if (
            self.target_plan.current_stage != "applied"
            and self.extraction.projected_state is not None
            and self.extraction.projected_state.stage == "applied"
            and expected_applied_date is None
        ):
            expected_applied_date = (
                self.extraction.history.date if self.extraction.history else None
            )
        if self.target_plan.projected_applied_date != expected_applied_date:
            raise ValueError("target plan projected applied date must match extracted effect")
        expected_channel = self.extraction.channel or self.target_plan.current_channel
        if self.target_plan.projected_channel != expected_channel:
            raise ValueError("target plan projected channel must match extracted effect")
        return self


class ReviewRecordOperationError(BaseModel):
    """Sanitized terminal error; provider text and exception details are never persisted."""

    model_config = ConfigDict(extra="forbid", strict=True)

    code: Literal[
        "extract_failed",
        "extract_cancelled",
        "publish_failed",
        "interrupted",
        "target_changed",
        "source_changed",
        "contract_invalid",
    ]
    message: ErrorText


class ReviewRecordOperationDTO(BaseModel):
    """Tenant-scoped canonical operation read model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["review_record"]
    contract_version: Literal[REVIEW_RECORD_CONTRACT_VERSION]
    operation_id: UUIDText
    review_reference: UUIDText
    client_turn_id: UUIDText
    mode: Literal["initial", "supplement"]
    state: Literal[
        "processing", "pending_confirmation", "completed", "rejected", "failed", "superseded",
    ]
    terminal: bool
    outcome: Literal["applied", "needs_clarification"] | None = None
    created_time: TimestampText
    finished_time: TimestampText | None = None
    source_journal_id: int = Field(gt=0)
    target_journal_id: int = Field(gt=0)
    target_current_state: Literal[
        "pending", "awaiting_user", "applied", "failed", "superseded", "voided",
    ]
    target_current_revision: int = Field(ge=0)
    preview: ReviewRecordPreview | None = None
    result: ReviewRecordResult | None = None
    error: ReviewRecordOperationError | None = None
    undo_available: bool
    undo_block_reason: Literal[
        "operation_not_applied", "target_changed", "target_not_applied",
    ] | None = None

    @field_validator("operation_id", "review_reference", "client_turn_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        return _canonical_uuid(value)

    @model_validator(mode="after")
    def validate_state_shape(self) -> "ReviewRecordOperationDTO":
        if self.state == "processing":
            if self.terminal or any(value is not None for value in (
                self.outcome,
                self.finished_time,
                self.preview,
                self.result,
                self.error,
            )):
                raise ValueError("processing operation cannot expose a terminal payload")
        elif self.state == "pending_confirmation":
            if self.terminal or self.preview is None or any(value is not None for value in (
                self.outcome,
                self.finished_time,
                self.result,
                self.error,
            )):
                raise ValueError("pending confirmation requires only one safe preview")
        elif not self.terminal or self.finished_time is None:
            raise ValueError("terminal operation requires terminal=true and finished_time")
        elif self.state == "completed":
            if (
                self.preview is not None
                or self.result is None
                or self.error is not None
                or self.outcome != self.result.outcome
            ):
                raise ValueError("completed operation requires one matching result")
        elif self.state == "rejected":
            if any(value is not None for value in (
                self.preview,
                self.result,
                self.outcome,
                self.error,
            )):
                raise ValueError("rejected operation cannot expose a result or error")
        elif (
            self.preview is not None
            or self.result is not None
            or self.outcome is not None
            or self.error is None
        ):
            raise ValueError("failed/superseded operation requires only an error")

        if self.state != "completed" or self.outcome != "applied" or self.result is None:
            expected_available = False
            expected_reason = "operation_not_applied"
        elif self.target_current_state != "applied":
            expected_available = False
            expected_reason = "target_not_applied"
        elif self.target_current_revision != self.result.target_revision:
            expected_available = False
            expected_reason = "target_changed"
        else:
            expected_available = True
            expected_reason = None
        if (
            self.undo_available != expected_available
            or self.undo_block_reason != expected_reason
        ):
            raise ValueError("record undo availability disagrees with current target")
        return self
