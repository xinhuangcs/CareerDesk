"""Review API for trusted records/corrections and page-approved whole-review undo."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...auth import current_user_id
from ...core.config import get_settings
from . import operations
from .operations import edit as edit_operations
from .operations import record as record_operations
from .operations.edit_models import (
    ReviewTimelineEntryEditOperationDTO,
    ReviewTimelineEntryEditUndoCommandStatus,
)
from .operations.record_models import (
    MAX_REVIEW_RECORD_BATCH_DECISION_CHARS,
    MAX_REVIEW_RECORD_OPERATIONS_PER_TURN,
    ReviewRecordDecision,
    ReviewRecordOperationDTO,
)
from .operations.undo_models import ReviewOperationDTO

router = APIRouter(prefix="/api/reviews")


class ReviewOperationCommand(BaseModel):
    """Approval/keep has no business fields, preventing injected targets."""

    model_config = ConfigDict(extra="forbid")


class ReviewTimelineEntryEditUndoRequest(BaseModel):
    """Browser-issued command UUID reused after an uncertain response."""

    model_config = ConfigDict(extra="forbid")

    command_id: UUID


class ReviewTimelineEntryEditOperationsResponse(BaseModel):
    """Review event correction receipts from one trusted browser turn."""

    model_config = ConfigDict(extra="forbid")

    operations: list[ReviewTimelineEntryEditOperationDTO] = Field(max_length=20)


class ReviewRecordOperationsResponse(BaseModel):
    """Isolated review record operations from one trusted browser turn."""

    model_config = ConfigDict(extra="forbid")

    operations: list[ReviewRecordOperationDTO] = Field(
        max_length=MAX_REVIEW_RECORD_OPERATIONS_PER_TURN,
    )


class ReviewRecordBatchDecisionRequest(BaseModel):
    """One complete browser-confirmed decision for a Review turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    decisions: list[ReviewRecordDecision] = Field(
        min_length=1,
        max_length=MAX_REVIEW_RECORD_OPERATIONS_PER_TURN,
    )

    @model_validator(mode="after")
    def unique_operation_ids(self) -> "ReviewRecordBatchDecisionRequest":
        operation_ids = [decision.operation_id for decision in self.decisions]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("decisions 不能包含重复的 operation_id")
        edited_chars = sum(
            len(decision.edited_extraction.model_dump_json())
            for decision in self.decisions
            if decision.edited_extraction is not None
        )
        if edited_chars > MAX_REVIEW_RECORD_BATCH_DECISION_CHARS:
            raise ValueError("批量岗位编辑内容超过安全上限")
        return self


class PendingReviewRecordClarificationsResponse(BaseModel):
    """Current review needing user detail after refresh or cross-tab recovery."""

    model_config = ConfigDict(extra="forbid")

    operations: list[ReviewRecordOperationDTO] = Field(max_length=100)


class PendingReviewRecordConfirmationsResponse(BaseModel):
    """Review preview awaiting selection/rejection across refreshes."""

    model_config = ConfigDict(extra="forbid")

    operations: list[ReviewRecordOperationDTO] = Field(max_length=100)


class PendingReviewUndoOperationsResponse(BaseModel):
    """Whole-review undo preview still eligible for page approval."""

    model_config = ConfigDict(extra="forbid")

    operations: list[ReviewOperationDTO] = Field(max_length=100)


class TimelineReviewUndoPrepareRequest(BaseModel):
    """Freeze the exact Review timeline-entry snapshot selected on Timeline."""

    model_config = ConfigDict(extra="forbid")

    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def _raise_operation_error(error: Exception) -> None:
    if isinstance(error, operations.ReviewOperationNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, operations.ReviewOperationConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise error


def _raise_timeline_entry_edit_error(error: Exception) -> None:
    if isinstance(error, edit_operations.ReviewTimelineEntryEditOperationNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, edit_operations.ReviewTimelineEntryEditOperationConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise error


def _raise_record_error(error: Exception) -> None:
    if isinstance(error, record_operations.ReviewRecordOperationNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, record_operations.ReviewRecordOperationConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise error


@router.get("/record-operations/by-client-turn/{client_turn_id}")
def get_review_record_operations_by_client_turn(
    client_turn_id: UUID,
    user_id: str = Depends(current_user_id),
) -> ReviewRecordOperationsResponse:
    try:
        operations_for_turn = record_operations.list_review_record_operations_for_turn(
            get_settings().db_path,
            user_id,
            client_turn_id,
        )
    except record_operations.ReviewRecordOperationConflict as error:
        _raise_record_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    return ReviewRecordOperationsResponse(operations=operations_for_turn)


@router.post(
    "/record-operations/by-client-turn/{client_turn_id}/decide",
    response_model=ReviewRecordOperationsResponse,
)
def decide_review_record_operations_by_client_turn(
    client_turn_id: UUID,
    payload: ReviewRecordBatchDecisionRequest,
    user_id: str = Depends(current_user_id),
) -> ReviewRecordOperationsResponse:
    try:
        decided = record_operations.decide_review_record_operations_for_turn(
            get_settings().db_path,
            user_id,
            client_turn_id,
            payload.decisions,
        )
    except (
        record_operations.ReviewRecordOperationNotFound,
        record_operations.ReviewRecordOperationConflict,
    ) as error:
        _raise_record_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    return ReviewRecordOperationsResponse(operations=decided)


@router.get("/record-operations/pending-clarifications")
def pending_review_record_clarifications(
    user_id: str = Depends(current_user_id),
) -> PendingReviewRecordClarificationsResponse:
    try:
        pending = record_operations.list_pending_review_record_clarifications(
            get_settings().db_path,
            user_id,
        )
    except record_operations.ReviewRecordOperationConflict as error:
        _raise_record_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    return PendingReviewRecordClarificationsResponse(operations=pending)


@router.get("/record-operations/pending-confirmations")
def pending_review_record_confirmations(
    user_id: str = Depends(current_user_id),
) -> PendingReviewRecordConfirmationsResponse:
    try:
        pending = record_operations.list_pending_review_record_confirmations(
            get_settings().db_path,
            user_id,
        )
    except record_operations.ReviewRecordOperationConflict as error:
        _raise_record_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    return PendingReviewRecordConfirmationsResponse(operations=pending)


@router.get("/record-operations/{operation_id}")
def get_review_record_operation(
    operation_id: UUID,
    user_id: str = Depends(current_user_id),
) -> ReviewRecordOperationDTO:
    try:
        operation = record_operations.get_review_record_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except record_operations.ReviewRecordOperationConflict as error:
        _raise_record_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    if operation is None:
        raise HTTPException(status_code=404, detail="复盘记录操作不存在")
    return ReviewRecordOperationDTO.model_validate(operation)


@router.post(
    "/record-operations/{operation_id}/approve",
    response_model=ReviewRecordOperationDTO,
)
def approve_review_record(
    operation_id: UUID,
    payload: ReviewOperationCommand,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return record_operations.approve_review_record_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except (
        record_operations.ReviewRecordOperationNotFound,
        record_operations.ReviewRecordOperationConflict,
    ) as error:
        _raise_record_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.post(
    "/record-operations/{operation_id}/reject",
    response_model=ReviewRecordOperationDTO,
)
def reject_review_record(
    operation_id: UUID,
    payload: ReviewOperationCommand,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return record_operations.reject_review_record_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except (
        record_operations.ReviewRecordOperationNotFound,
        record_operations.ReviewRecordOperationConflict,
    ) as error:
        _raise_record_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.post(
    "/record-operations/{operation_id}/prepare-undo",
    response_model=ReviewOperationDTO,
)
def prepare_review_record_undo(
    operation_id: UUID,
    payload: ReviewOperationCommand,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return record_operations.prepare_review_record_undo_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except (
        record_operations.ReviewRecordOperationNotFound,
        record_operations.ReviewRecordOperationConflict,
        operations.ReviewOperationConflict,
    ) as error:
        if isinstance(error, operations.ReviewOperationConflict):
            _raise_operation_error(error)
        else:
            _raise_record_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.get("/timeline-entry-edit-operations/by-client-turn/{client_turn_id}")
def get_review_timeline_entry_edit_operations_by_client_turn(
    client_turn_id: UUID,
    user_id: str = Depends(current_user_id),
) -> ReviewTimelineEntryEditOperationsResponse:
    try:
        operations_for_turn = edit_operations.list_review_timeline_entry_edit_operations_for_turn(
            get_settings().db_path,
            user_id,
            client_turn_id,
        )
    except edit_operations.ReviewTimelineEntryEditOperationConflict as error:
        _raise_timeline_entry_edit_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    return ReviewTimelineEntryEditOperationsResponse(operations=operations_for_turn)


@router.get("/timeline-entry-edit-operations/{operation_id}")
def get_review_timeline_entry_edit_operation(
    operation_id: UUID,
    user_id: str = Depends(current_user_id),
) -> ReviewTimelineEntryEditOperationDTO:
    try:
        operation = edit_operations.get_review_timeline_entry_edit_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except edit_operations.ReviewTimelineEntryEditOperationConflict as error:
        _raise_timeline_entry_edit_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    if operation is None:
        raise HTTPException(status_code=404, detail="复盘历程修正操作不存在")
    return ReviewTimelineEntryEditOperationDTO.model_validate(operation)


@router.get("/timeline-entry-edit-undo-commands/{command_id}")
def get_review_timeline_entry_edit_undo_command(
    command_id: UUID,
    user_id: str = Depends(current_user_id),
) -> ReviewTimelineEntryEditUndoCommandStatus:
    try:
        status = edit_operations.get_review_timeline_entry_edit_undo_command_status(
            get_settings().db_path,
            user_id,
            command_id,
        )
        return ReviewTimelineEntryEditUndoCommandStatus.model_validate(status)
    except edit_operations.ReviewTimelineEntryEditOperationConflict as error:
        _raise_timeline_entry_edit_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.post("/timeline-entry-edit-operations/{operation_id}/undo")
def undo_review_timeline_entry_edit_operation(
    operation_id: UUID,
    payload: ReviewTimelineEntryEditUndoRequest,
    user_id: str = Depends(current_user_id),
) -> ReviewTimelineEntryEditOperationDTO:
    try:
        operation = edit_operations.undo_review_timeline_entry_edit_operation(
            get_settings().db_path,
            user_id,
            operation_id,
            command_id=payload.command_id,
        )
        return ReviewTimelineEntryEditOperationDTO.model_validate(operation)
    except (
        edit_operations.ReviewTimelineEntryEditOperationNotFound,
        edit_operations.ReviewTimelineEntryEditOperationConflict,
    ) as error:
        _raise_timeline_entry_edit_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.get(
    "/undo-operations/pending",
    response_model=PendingReviewUndoOperationsResponse,
)
def pending_review_undo_operations(
    user_id: str = Depends(current_user_id),
) -> dict:
    """Return exact review undo previews awaiting the current user's approval."""
    return {
        "operations": operations.list_pending_review_operations(
            get_settings().db_path,
            user_id,
        ),
    }


@router.post(
    "/timeline-applications/{application_id}/timeline-entries/{timeline_entry_id}/prepare-undo",
    response_model=ReviewOperationDTO,
)
def prepare_timeline_review_undo_operation(
    application_id: int,
    timeline_entry_id: int,
    payload: TimelineReviewUndoPrepareRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Create a server-frozen proposal; actual deletion still requires approval."""
    try:
        return operations.prepare_review_timeline_entry_undo_operation(
            get_settings().db_path,
            user_id,
            application_id,
            timeline_entry_id,
            expected_fingerprint=payload.expected_fingerprint,
        )
    except (
        operations.ReviewOperationNotFound,
        operations.ReviewOperationConflict,
    ) as error:
        _raise_operation_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.get("/undo-operations/{operation_id}", response_model=ReviewOperationDTO)
def get_review_undo_operation(
    operation_id: UUID,
    user_id: str = Depends(current_user_id),
) -> dict:
    operation = operations.get_review_operation(
        get_settings().db_path,
        user_id,
        operation_id,
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="复盘撤销操作不存在")
    return operation


@router.post(
    "/undo-operations/{operation_id}/approve",
    response_model=ReviewOperationDTO,
)
def approve_review_undo_operation(
    operation_id: UUID,
    payload: ReviewOperationCommand,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return operations.approve_review_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except (operations.ReviewOperationNotFound,
            operations.ReviewOperationConflict) as error:
        _raise_operation_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.post(
    "/undo-operations/{operation_id}/reject",
    response_model=ReviewOperationDTO,
)
def reject_review_undo_operation(
    operation_id: UUID,
    payload: ReviewOperationCommand,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return operations.reject_review_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except (operations.ReviewOperationNotFound,
            operations.ReviewOperationConflict) as error:
        _raise_operation_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
