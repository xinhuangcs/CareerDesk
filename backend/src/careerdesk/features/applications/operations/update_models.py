"""Bounded durable contracts and DTOs for ordinary application corrections."""

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ....platform.database import normalize_application_identity_part
from ..intake_models import (
    ApplicationNextAction,
    ApplicationStage,
    CompanyText,
    ISODateText,
    JDText,
    NextNoteText,
    PositionText,
    StepText,
)

APPLICATION_UPDATE_CONTRACT_VERSION = 1
MAX_UPDATE_QUESTIONS = 100
MAX_UPDATE_OCCURRENCES = 1_000

UpdateField = Literal[
    "company",
    "position",
    "stage",
    "current_step",
    "priority",
    "next_action",
    "application_note",
    "jd_text",
]
UUIDText = Annotated[
    str,
    StringConstraints(pattern=(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}$"
    )),
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BoundedTimestamp = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]


class ApplicationUpdateChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    company: CompanyText | None = None
    position: PositionText | None = None
    stage: ApplicationStage | None = None
    current_step: StepText | None = None
    priority: Literal["high", "medium", "low"] | None = None
    next_action: ApplicationNextAction | None = None
    application_note: NextNoteText | None = None
    jd_text: JDText | None = None

    @model_validator(mode="after")
    def require_change(self) -> "ApplicationUpdateChanges":
        if not self.model_fields_set:
            raise ValueError("application update requires at least one changed field")
        for field in ("company", "position", "stage"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be cleared")
        return self


class ApplicationUpdateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    company: CompanyText
    position: PositionText | None = None
    changes: ApplicationUpdateChanges
    expected_application_id: int | None = Field(default=None, gt=0)
    expected_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_expected_projection(self) -> "ApplicationUpdateCommand":
        if (self.expected_application_id is None) != (self.expected_revision is None):
            raise ValueError("expected application identity and revision must be provided together")
        return self


class ApplicationUpdateTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    application_id: int = Field(gt=0)
    company: CompanyText
    position: PositionText
    application_created_time: BoundedTimestamp


class ApplicationUpdateProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    company: CompanyText
    company_id: int | None = Field(default=None, gt=0)
    position: PositionText
    stage: ApplicationStage
    current_step: StepText | None = None
    priority: Literal["high", "medium", "low"] | None = None
    applied_date: ISODateText | None = None
    next_action: ApplicationNextAction | None = None
    paused_from_stage: ApplicationStage | None = None
    pause_reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000),
    ] | None = None
    application_note: NextNoteText | None = None
    jd_text: JDText | None = None
    revision: int = Field(ge=0)
    application_updated_time: BoundedTimestamp

    @field_validator("applied_date")
    @classmethod
    def validate_applied_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("application applied date must be a real ISO date") from error
        if parsed.isoformat() != value:
            raise ValueError("application applied date must be a real ISO date")
        return value


class ApplicationUpdateFieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    field: UpdateField
    before: str | ApplicationNextAction | None
    after: str | ApplicationNextAction | None

    @model_validator(mode="after")
    def require_semantic_change(self) -> "ApplicationUpdateFieldChange":
        if self.before == self.after:
            raise ValueError("application update field effect must change its value")
        return self


class ApplicationUpdateQuestionProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    question_created_time: BoundedTimestamp
    before_updated_time: BoundedTimestamp
    after_updated_time: BoundedTimestamp
    before_company: CompanyText | None = None
    after_company: CompanyText


class ApplicationUpdateOccurrenceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    journal_id: int = Field(gt=0)
    question_id: int = Field(gt=0)
    application_id: int = Field(gt=0)
    journal_created_time: BoundedTimestamp
    journal_state: Literal["applied"]
    journal_revision: int = Field(ge=0)
    question_created_time: BoundedTimestamp
    before_company: CompanyText
    after_company: CompanyText
    source_step: str | None = Field(default=None, max_length=300)
    asked_date: str | None = Field(default=None, max_length=64)


class ApplicationUpdateEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    changed_fields: list[ApplicationUpdateFieldChange] = Field(min_length=1, max_length=7)
    question_provenance: list[ApplicationUpdateQuestionProvenance] = Field(
        default_factory=list,
        max_length=MAX_UPDATE_QUESTIONS,
    )
    question_occurrences: list[ApplicationUpdateOccurrenceProvenance] = Field(
        default_factory=list,
        max_length=MAX_UPDATE_OCCURRENCES,
    )
    prep_invalidated: bool
    prep_restored_on_undo: Literal[False]
    company_record_created: bool
    company_records_retained_on_undo: Literal[True]

    @model_validator(mode="after")
    def validate_effect(self) -> "ApplicationUpdateEffect":
        fields = [change.field for change in self.changed_fields]
        if len(fields) != len(set(fields)):
            raise ValueError("application update fields must be unique")
        question_ids = [item.id for item in self.question_provenance]
        occurrence_ids = [
            (item.journal_id, item.question_id) for item in self.question_occurrences
        ]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("application update question ids must be unique")
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise ValueError("application update occurrence ids must be unique")
        renamed_company = "company" in fields
        if renamed_company != bool(
            self.question_provenance or self.question_occurrences
        ) and (self.question_provenance or self.question_occurrences):
            raise ValueError("only company rename may change provenance")
        if self.company_record_created and not renamed_company:
            raise ValueError("only company rename may create a company record")
        if self.prep_invalidated != bool({"company", "position", "jd_text"} & set(fields)):
            raise ValueError("prep invalidation must exactly follow identity or JD change")
        return self


class ApplicationUpdateProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["application_update"]
    contract_version: Literal[APPLICATION_UPDATE_CONTRACT_VERSION]
    client_turn_id: UUIDText
    request_digest: Digest
    occurred_date: ISODateText
    target: ApplicationUpdateTarget
    before: ApplicationUpdateProjection
    final: ApplicationUpdateProjection
    effect: ApplicationUpdateEffect

    @field_validator("occurred_date")
    @classmethod
    def validate_occurred_date(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("application update date must be a real ISO date") from error
        if parsed.isoformat() != value:
            raise ValueError("application update date must be a real ISO date")
        return value

    @model_validator(mode="after")
    def validate_projection(self) -> "ApplicationUpdateProposal":
        if (
            self.target.company != self.before.company
            or self.target.position != self.before.position
        ):
            raise ValueError("application update target disagrees with before identity")
        if self.final.revision != self.before.revision + 1:
            raise ValueError("application update revision must advance exactly once")
        expected = {
            change.field: change.after for change in self.effect.changed_fields
        }
        before = {
            change.field: change.before for change in self.effect.changed_fields
        }
        for field in (
            "company", "position", "stage", "current_step", "priority", "next_action",
            "application_note", "jd_text",
        ):
            before_value = getattr(self.before, field)
            final_value = getattr(self.final, field)
            if field in expected:
                if before_value != before[field] or final_value != expected[field]:
                    raise ValueError("application update effect disagrees with projections")
            elif before_value != final_value:
                raise ValueError("application update changed an undisclosed field")
        applied_date_initialized = (
            "stage" in expected
            and self.before.stage != "applied"
            and self.final.stage == "applied"
            and self.before.applied_date is None
        )
        if applied_date_initialized:
            if self.final.applied_date != self.occurred_date:
                raise ValueError("first applied transition must use the operation date")
        elif self.final.applied_date != self.before.applied_date:
            raise ValueError("application update changed applied date unexpectedly")
        if "stage" not in expected and any(
            getattr(self.before, field) != getattr(self.final, field)
            for field in ("paused_from_stage", "pause_reason")
        ):
            raise ValueError("non-stage update changed pause metadata")
        if "stage" in expected:
            if self.final.stage == "pooled":
                expected_paused_from = (
                    self.before.stage
                    if self.before.stage in {
                        "backlog", "applied", "written_test", "interviewing", "offer",
                    }
                    else None
                )
                if (
                    self.final.paused_from_stage != expected_paused_from
                    or self.final.pause_reason is not None
                ):
                    raise ValueError("entering pooled produced inconsistent pause metadata")
            elif self.final.paused_from_stage is not None or self.final.pause_reason is not None:
                raise ValueError("non-pooled stage cannot retain pause metadata")
        if self.final.stage in {"rejected", "withdrawn"}:
            if self.final.next_action is not None:
                raise ValueError("closed stage cannot retain a next action")
        elif "next_action" not in expected and self.final.next_action != self.before.next_action:
            raise ValueError("application update changed an undisclosed next action")
        if "company" not in expected and self.before.company_id != self.final.company_id:
            raise ValueError("non-company update changed company identity")
        company_changed = "company" in expected
        if company_changed:
            same_company_identity = (
                normalize_application_identity_part(self.before.company)
                == normalize_application_identity_part(self.final.company)
            )
            if self.final.company_id is None:
                raise ValueError("company rename requires a final company identity")
            if (
                same_company_identity
                and self.before.company_id is not None
                and self.final.company_id != self.before.company_id
            ):
                raise ValueError("display-only company rename changed company identity")
            if (
                not same_company_identity
                and self.final.company_id == self.before.company_id
            ):
                raise ValueError("company identity rename did not rebind company identity")
            if any(
                item.after_company != self.final.company
                or item.after_updated_time != self.final.application_updated_time
                for item in self.effect.question_provenance
            ) or any(
                item.after_company != self.final.company
                or item.application_id != self.target.application_id
                for item in self.effect.question_occurrences
            ):
                raise ValueError("company rename provenance disagrees with final identity")
        elif self.effect.question_provenance or self.effect.question_occurrences:
            raise ValueError("non-company update cannot expose provenance changes")
        if self.effect.company_record_created and (
            not company_changed
            or (
                self.before.company_id is not None
                and self.final.company_id == self.before.company_id
            )
        ):
            raise ValueError("created company record must be the new final identity")
        return self


class ApplicationUpdateApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["ok"]
    application_id: int = Field(gt=0)
    revision: int = Field(ge=1)
    timeline_entry_id: int | None = Field(default=None, gt=0)
    questions_updated: int = Field(ge=0, le=MAX_UPDATE_QUESTIONS)
    question_occurrences_updated: int = Field(ge=0, le=MAX_UPDATE_OCCURRENCES)
    prep_invalidated: bool


class ApplicationUpdateUndoResult(ApplicationUpdateApplyResult):
    pass


class ApplicationUpdateReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    apply: ApplicationUpdateApplyResult
    undo: ApplicationUpdateUndoResult | None = None


UndoBlockReason = Literal[
    "target_missing",
    "target_changed",
    "prep_changed",
    "provenance_changed",
    "natural_key_taken",
    "operation_invalid",
    "already_undone",
]

ApplicationUpdateUndoErrorCode = Literal[
    "operation_not_found",
    "operation_invalid",
    "target_missing",
    "target_changed",
    "prep_changed",
    "provenance_changed",
    "natural_key_taken",
]


class ApplicationUpdateUndoCommandError(BaseModel):
    """Undo rejection reason safe for response and durable replay."""

    model_config = ConfigDict(extra="forbid", strict=True)

    code: ApplicationUpdateUndoErrorCode
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
    ]


class ApplicationUpdateUndoCommandStatus(BaseModel):
    """Tenant-scoped terminal undo state; absent is the sole nonterminal value."""

    model_config = ConfigDict(extra="forbid", strict=True)

    command_id: UUIDText
    operation_id: UUIDText | None = None
    state: Literal["absent", "completed", "rejected"]
    terminal: bool
    error: ApplicationUpdateUndoCommandError | None = None
    finished_time: BoundedTimestamp | None = None

    @model_validator(mode="after")
    def validate_terminal_receipt(self) -> "ApplicationUpdateUndoCommandStatus":
        if self.state == "absent":
            if (
                self.terminal
                or self.operation_id is not None
                or self.error is not None
                or self.finished_time is not None
            ):
                raise ValueError("absent undo command cannot expose a terminal receipt")
        elif self.state == "completed":
            if (
                not self.terminal
                or self.operation_id is None
                or self.error is not None
                or self.finished_time is None
            ):
                raise ValueError("completed undo command receipt is inconsistent")
        elif (
            not self.terminal
            or self.operation_id is None
            or self.error is None
            or self.finished_time is None
        ):
            raise ValueError("rejected undo command receipt is inconsistent")
        return self


class ApplicationUpdateOperationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: UUIDText
    operation_type: Literal["application_update"]
    contract_version: Literal[APPLICATION_UPDATE_CONTRACT_VERSION]
    state: Literal["completed", "undone", "stale"]
    created_time: BoundedTimestamp
    client_turn_id: UUIDText
    target: ApplicationUpdateTarget
    before: ApplicationUpdateProjection
    final: ApplicationUpdateProjection
    effect: ApplicationUpdateEffect
    result: ApplicationUpdateReceipt | None
    undo_available: bool
    undo_block_reason: UndoBlockReason | None

    @model_validator(mode="after")
    def validate_state(self) -> "ApplicationUpdateOperationDTO":
        if self.state == "completed":
            if self.result is None or self.result.undo is not None:
                raise ValueError("completed update must expose only the apply receipt")
            if self.undo_available == (self.undo_block_reason is not None):
                raise ValueError("completed update undoability is inconsistent")
        elif self.state == "undone":
            if self.result is None or self.result.undo is None:
                raise ValueError("undone update must expose both receipts")
            if self.undo_available or self.undo_block_reason != "already_undone":
                raise ValueError("undone update cannot remain undoable")
        else:
            if self.result is not None or self.undo_available:
                raise ValueError("stale update cannot expose a trusted receipt")
            if self.undo_block_reason != "operation_invalid":
                raise ValueError("stale update must report invalid operation")
        if self.result is not None:
            apply = self.result.apply
            if (
                apply.application_id != self.target.application_id
                or apply.revision != self.final.revision
                or (apply.timeline_entry_id is not None) != (
                    bool(
                        {"stage", "current_step"}
                        & {item.field for item in self.effect.changed_fields}
                    )
                )
                or apply.questions_updated != len(self.effect.question_provenance)
                or apply.question_occurrences_updated != len(
                    self.effect.question_occurrences
                )
                or apply.prep_invalidated != self.effect.prep_invalidated
            ):
                raise ValueError("application update apply receipt disagrees with effect")
            undo = self.result.undo
            if undo is not None and (
                undo.application_id != self.target.application_id
                or undo.revision != self.final.revision + 1
                or (undo.timeline_entry_id is not None) != (
                    bool(
                        {"stage", "current_step"}
                        & {item.field for item in self.effect.changed_fields}
                    )
                )
                or undo.questions_updated != len(self.effect.question_provenance)
                or undo.question_occurrences_updated != len(
                    self.effect.question_occurrences
                )
                or undo.prep_invalidated != self.effect.prep_invalidated
            ):
                raise ValueError("application update undo receipt disagrees with effect")
        return self
