"""Application board, detail, schedule, and priority API; orchestration owns prep."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ...auth import current_user_id
from ...core.config import get_settings, local_today
from ..reviews import public as reviews
from . import repository
from .service import ApplicationService
from .workbook_intake import WORKBOOK_SUFFIXES, parse_standard_workbook
from .intake_models import MAX_BATCH_POSITIONS
from .contracts import (
    ApplicationCreateRequest,
    ApplicationDeleteOperationsResponse,
    ApplicationDetailResponse,
    ApplicationNextActionUpdateRequest,
    ApplicationProgressWriteRequest,
    ApplicationMergeOperationsResponse,
    ApplicationNoteRequest,
    ApplicationProfileUpdateRequest,
    ApplicationResumeBindingRequest,
    ApplicationResumeBindingResponse,
    ApplicationStageMoveRequest,
    CompleteNextActionRequest,
    BoardResponse,
    IntakeOperationDTO,
    IntakeOperationsResponse,
    TimelineEntryUpdateRequest,
    TimelineStatisticsResponse,
    UpcomingResponse,
)
from .operations import (
    ApplicationDeleteOperationConflict,
    ApplicationDeleteOperationNotFound,
    ApplicationMergeOperationConflict,
    ApplicationMergeOperationNotFound,
    ApplicationUpdateOperationConflict,
    ApplicationUpdateOperationNotFound,
    approve_application_delete_operation,
    approve_application_merge_operation,
    get_application_delete_operation,
    get_application_merge_operation,
    get_application_update_operation,
    get_application_update_undo_command_status,
    list_pending_application_delete_operations,
    list_pending_application_merge_operations,
    list_application_update_operations_for_turn,
    prepare_application_delete_operation,
    reject_application_delete_operation,
    reject_application_merge_operation,
    undo_application_update_operation,
)
from .operations.merge_models import ApplicationMergeOperationDTO
from .operations.models import ApplicationDeleteOperationDTO
from .operations.update_models import (
    ApplicationUpdateOperationDTO,
    ApplicationUpdateUndoCommandStatus,
)

router = APIRouter(prefix="/api/timeline")


class PriorityRequest(BaseModel):
    """Application priority request body."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    priority: Literal["high", "medium", "low"] | None


StrictPreviewIndex = Annotated[StrictInt, Field(ge=1)]


class IntakeApprovalRequest(BaseModel):
    """Page approval may exclude only one-based rows from the trusted preview."""

    model_config = ConfigDict(extra="forbid")

    exclude_indexes: list[StrictPreviewIndex] = Field(
        default_factory=list,
        max_length=MAX_BATCH_POSITIONS,
    )


class IntakeRejectionRequest(BaseModel):
    """Reject business-free payloads and model-controlled command fields."""

    model_config = ConfigDict(extra="forbid")


class WorkbookIntakeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["preview", "unrecognized", "empty", "superseded"]
    operation_id: UUID | None = None
    source_rows: int = Field(ge=0)
    skipped_rows: int = Field(ge=0)
    positions: list[dict] = Field(default_factory=list)


class ApplicationDeleteCommandRequest(BaseModel):
    """Delete approval/keep accepts only the server operation ID in the URL."""

    model_config = ConfigDict(extra="forbid")


class ApplicationMergeCommandRequest(BaseModel):
    """Merge approval/keep accepts only the server operation ID in the URL."""

    model_config = ConfigDict(extra="forbid")


class ApplicationUpdateUndoRequest(BaseModel):
    """Caller-issued command UUID reused by every retry of one user action."""

    model_config = ConfigDict(extra="forbid")

    command_id: UUID


class ApplicationUpdateOperationsResponse(BaseModel):
    """Ordinary application mutation receipt from one trusted browser turn."""

    model_config = ConfigDict(extra="forbid")

    operations: list[ApplicationUpdateOperationDTO] = Field(max_length=20)


@router.put(
    "/applications/{application_id}/resume-binding",
    response_model=ApplicationResumeBindingResponse,
)
def bind_application_resume(
    application_id: int,
    payload: ApplicationResumeBindingRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Persist the resume choice without disturbing an eligible research snapshot."""
    try:
        result = repository.bind_application_resume(
            get_settings().db_path,
            user_id,
            application_id,
            payload.resume_id,
            expected_edit_revision=payload.expected_edit_revision,
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    return result


def _raise_intake_error(error: Exception) -> None:
    if isinstance(error, repository.IntakeOperationNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, repository.IntakeOperationInvalidSelection):
        raise HTTPException(status_code=422, detail=str(error)) from error
    if isinstance(error, repository.IntakeOperationConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise error


def _raise_application_delete_error(error: Exception) -> None:
    if isinstance(error, ApplicationDeleteOperationNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ApplicationDeleteOperationConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise error


def _raise_application_merge_error(error: Exception) -> None:
    if isinstance(error, ApplicationMergeOperationNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ApplicationMergeOperationConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise error


def _raise_application_update_error(error: Exception) -> None:
    if isinstance(error, ApplicationUpdateOperationNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ApplicationUpdateOperationConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise error


@router.get("/intake-operations/pending", response_model=IntakeOperationsResponse)
def get_pending_intake_operations(
    user_id: str = Depends(current_user_id),
) -> dict:
    """Return every batch role preview awaiting the current user's decision."""
    operations = repository.list_pending_intake_operations(get_settings().db_path, user_id)
    return {"operations": operations}


@router.post(
    "/intake-operations/file",
    response_model=WorkbookIntakeResponse,
)
async def create_file_intake_operation(
    file: UploadFile = File(...),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Parse known workbook headers without AI or model-supplied operation IDs."""
    from tempfile import NamedTemporaryFile

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in WORKBOOK_SUFFIXES:
        raise HTTPException(status_code=422, detail="只支持 xlsx、xls、csv、tsv 表格")
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > 10 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="表格文件不能超过 10 MB")
                temporary.write(chunk)
        parsed = await asyncio.to_thread(parse_standard_workbook, temporary_path)
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    if not parsed.is_standard:
        return {
            "status": "unrecognized", "source_rows": parsed.source_rows,
            "skipped_rows": parsed.source_rows, "positions": [],
        }
    if not parsed.positions:
        return {
            "status": "empty", "source_rows": parsed.source_rows,
            "skipped_rows": parsed.skipped_rows, "positions": [],
        }
    try:
        result = ApplicationService(get_settings().db_path, None).parse_standard_positions(
            user_id,
            list(parsed.positions),
            source_label=f"表格导入：{Path(file.filename or '岗位表').name}",
            source_rows=parsed.source_rows,
            skipped_rows=parsed.skipped_rows,
        )
    except ValueError as error:
        # Batch text bounds and similar contract errors are correctable input, not 500s.
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "status": result["status"],
        "operation_id": result.get("operation_id"),
        "source_rows": parsed.source_rows,
        "skipped_rows": parsed.skipped_rows,
        "positions": result.get("positions", []),
    }


@router.get("/intake-operations/{operation_id}", response_model=IntakeOperationDTO)
def get_intake_operation(
    operation_id: UUID,
    user_id: str = Depends(current_user_id),
) -> dict:
    operation = repository.get_intake_operation(
        get_settings().db_path, user_id, operation_id,
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="批量导入操作不存在")
    return operation


@router.post("/intake-operations/{operation_id}/approve", response_model=IntakeOperationDTO)
def approve_intake_operation(
    operation_id: UUID,
    payload: IntakeApprovalRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    try:
        return repository.approve_intake_operation(
            get_settings().db_path,
            user_id,
            operation_id,
            exclude_indexes=payload.exclude_indexes,
        )
    except (repository.IntakeOperationNotFound,
            repository.IntakeOperationInvalidSelection,
            repository.IntakeOperationConflict) as error:
        _raise_intake_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.post("/intake-operations/{operation_id}/reject", response_model=IntakeOperationDTO)
def reject_intake_operation(
    operation_id: UUID,
    payload: IntakeRejectionRequest | None = None,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return repository.reject_intake_operation(
            get_settings().db_path, user_id, operation_id,
        )
    except (repository.IntakeOperationNotFound,
            repository.IntakeOperationConflict) as error:
        _raise_intake_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.get(
    "/application-delete-operations/pending",
    response_model=ApplicationDeleteOperationsResponse,
)
def get_pending_application_delete_operations(
    user_id: str = Depends(current_user_id),
) -> dict:
    """Return application deletion previews the current user may still approve."""
    try:
        operations = list_pending_application_delete_operations(
            get_settings().db_path,
            user_id,
        )
    except ApplicationDeleteOperationConflict as error:
        _raise_application_delete_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    return {"operations": operations}


@router.get(
    "/application-merge-operations/pending",
    response_model=ApplicationMergeOperationsResponse,
)
def get_pending_application_merge_operations(
    user_id: str = Depends(current_user_id),
) -> dict:
    """Return directed application merge previews still eligible for approval."""
    try:
        operations = list_pending_application_merge_operations(
            get_settings().db_path,
            user_id,
        )
    except ApplicationMergeOperationConflict as error:
        _raise_application_merge_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    return {"operations": operations}


@router.get("/application-update-operations/by-client-turn/{client_turn_id}")
def get_application_update_operations_by_client_turn(
    client_turn_id: UUID,
    user_id: str = Depends(current_user_id),
) -> ApplicationUpdateOperationsResponse:
    try:
        operations = list_application_update_operations_for_turn(
            get_settings().db_path,
            user_id,
            client_turn_id,
        )
    except ApplicationUpdateOperationConflict as error:
        _raise_application_update_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    return ApplicationUpdateOperationsResponse(operations=operations)


@router.get("/application-update-operations/{operation_id}")
def read_application_update_operation(
    operation_id: UUID,
    user_id: str = Depends(current_user_id),
) -> ApplicationUpdateOperationDTO:
    try:
        operation = get_application_update_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except ApplicationUpdateOperationConflict as error:
        _raise_application_update_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    if operation is None:
        raise HTTPException(status_code=404, detail="岗位修改操作不存在")
    return ApplicationUpdateOperationDTO.model_validate(operation)


@router.get("/application-update-undo-commands/{command_id}")
def read_application_update_undo_command(
    command_id: UUID,
    user_id: str = Depends(current_user_id),
) -> ApplicationUpdateUndoCommandStatus:
    try:
        status = get_application_update_undo_command_status(
            get_settings().db_path,
            user_id,
            command_id,
        )
        return ApplicationUpdateUndoCommandStatus.model_validate(status)
    except ApplicationUpdateOperationConflict as error:
        _raise_application_update_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.post("/application-update-operations/{operation_id}/undo")
def undo_application_update(
    operation_id: UUID,
    payload: ApplicationUpdateUndoRequest,
    user_id: str = Depends(current_user_id),
) -> ApplicationUpdateOperationDTO:
    try:
        operation = undo_application_update_operation(
            get_settings().db_path,
            user_id,
            operation_id,
            command_id=payload.command_id,
        )
        return ApplicationUpdateOperationDTO.model_validate(operation)
    except (ApplicationUpdateOperationNotFound,
            ApplicationUpdateOperationConflict) as error:
        _raise_application_update_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.get(
    "/application-merge-operations/{operation_id}",
    response_model=ApplicationMergeOperationDTO,
)
def read_application_merge_operation(
    operation_id: UUID,
    user_id: str = Depends(current_user_id),
) -> dict:
    operation = get_application_merge_operation(
        get_settings().db_path,
        user_id,
        operation_id,
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="岗位合并操作不存在")
    return operation


@router.post(
    "/application-merge-operations/{operation_id}/approve",
    response_model=ApplicationMergeOperationDTO,
)
def approve_application_merge(
    operation_id: UUID,
    payload: ApplicationMergeCommandRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return approve_application_merge_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except (ApplicationMergeOperationNotFound,
            ApplicationMergeOperationConflict) as error:
        _raise_application_merge_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.post(
    "/application-merge-operations/{operation_id}/reject",
    response_model=ApplicationMergeOperationDTO,
)
def reject_application_merge(
    operation_id: UUID,
    payload: ApplicationMergeCommandRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return reject_application_merge_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except (ApplicationMergeOperationNotFound,
            ApplicationMergeOperationConflict) as error:
        _raise_application_merge_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.get(
    "/application-delete-operations/{operation_id}",
    response_model=ApplicationDeleteOperationDTO,
)
def read_application_delete_operation(
    operation_id: UUID,
    user_id: str = Depends(current_user_id),
) -> dict:
    operation = get_application_delete_operation(
        get_settings().db_path,
        user_id,
        operation_id,
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="岗位删除操作不存在")
    return operation


@router.post(
    "/application-delete-operations/{operation_id}/approve",
    response_model=ApplicationDeleteOperationDTO,
)
def approve_application_delete(
    operation_id: UUID,
    payload: ApplicationDeleteCommandRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return approve_application_delete_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except (ApplicationDeleteOperationNotFound,
            ApplicationDeleteOperationConflict) as error:
        _raise_application_delete_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.post(
    "/application-delete-operations/{operation_id}/reject",
    response_model=ApplicationDeleteOperationDTO,
)
def reject_application_delete(
    operation_id: UUID,
    payload: ApplicationDeleteCommandRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    del payload
    try:
        return reject_application_delete_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except (ApplicationDeleteOperationNotFound,
            ApplicationDeleteOperationConflict) as error:
        _raise_application_delete_error(error)
        raise AssertionError("unreachable")  # pragma: no cover


@router.get("/board", response_model=BoardResponse)
def get_board(user_id: str = Depends(current_user_id)) -> dict:
    """Return all applications grouped by stage for the primary board."""
    return repository.board(get_settings().db_path, user_id)


@router.get("/statistics", response_model=TimelineStatisticsResponse)
def get_statistics(user_id: str = Depends(current_user_id)) -> dict:
    """Return application outcomes and historically reached funnel stages."""
    return repository.statistics(get_settings().db_path, user_id)


@router.post(
    "/applications",
    response_model=ApplicationDetailResponse,
    status_code=201,
)
def create_application(
    payload: ApplicationCreateRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Create from Timeline, using the user's local date for an applied role."""
    settings = get_settings()
    try:
        application_id = repository.create_application_profile(
            settings.db_path,
            user_id,
            timezone_name=settings.timezone,
            **payload.model_dump(),
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    detail = repository.application_detail(
        settings.db_path,
        user_id,
        application_id,
        timezone_name=settings.timezone,
    )
    if detail is None:  # pragma: no cover - same command transaction invariant
        raise HTTPException(status_code=404, detail="application not found")
    return detail


@router.get("/applications/{application_id}", response_model=ApplicationDetailResponse)
def get_application(application_id: int, user_id: str = Depends(current_user_id)) -> dict:
    """Return complete application identity and factual history."""
    settings = get_settings()
    detail = repository.application_detail(
        settings.db_path, user_id, application_id, timezone_name=settings.timezone,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="application not found")
    return detail


@router.get("/upcoming", response_model=UpcomingResponse)
def get_upcoming(days: int = Query(default=7, ge=1, le=60),
                       user_id: str = Depends(current_user_id)) -> dict:
    """Return applications with next actions in the coming N days."""
    today = local_today()
    items = repository.upcoming(get_settings().db_path, user_id,
                                today.isoformat(),
                                (today + timedelta(days=days - 1)).isoformat())
    return {"days": days, "items": items}


@router.put("/applications/{application_id}/priority", response_model=ApplicationDetailResponse)
def set_priority(application_id: int, payload: PriorityRequest,
                   user_id: str = Depends(current_user_id)) -> dict:
    """Set or clear priority without triggering background computation."""
    settings = get_settings()
    try:
        updated = repository.set_priority(
            settings.db_path,
            user_id,
            application_id,
            payload.priority,
            expected_revision=payload.expected_revision,
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not updated:
        raise HTTPException(status_code=404, detail="application not found")
    detail = repository.application_detail(
        settings.db_path,
        user_id,
        application_id,
        timezone_name=settings.timezone,
    )
    if detail is None:  # pragma: no cover - same command snapshot invariant
        raise HTTPException(status_code=404, detail="application not found")
    return detail


@router.put(
    "/applications/{application_id}/note",
    response_model=ApplicationDetailResponse,
)
def update_application_note(
    application_id: int,
    payload: ApplicationNoteRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Create, edit, or clear the single application note under the shared CAS."""
    settings = get_settings()
    try:
        result = repository.set_application_note(
            settings.db_path,
            user_id,
            application_id,
            payload.note,
            expected_revision=payload.expected_revision,
            timezone_name=settings.timezone,
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    return result


@router.put(
    "/applications/{application_id}/profile",
    response_model=ApplicationDetailResponse,
)
def update_application_profile(
    application_id: int,
    payload: ApplicationProfileUpdateRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Edit base fields; notes and history retain separate concurrency-safe commands."""
    settings = get_settings()
    try:
        result = repository.update_application_profile(
            settings.db_path,
            user_id,
            application_id,
            timezone_name=settings.timezone,
            **payload.model_dump(exclude_unset=True),
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    detail = repository.application_detail(
        settings.db_path,
        user_id,
        application_id,
        timezone_name=settings.timezone,
    )
    if detail is None:  # pragma: no cover - same command snapshot invariant
        raise HTTPException(status_code=404, detail="application not found")
    return detail


@router.post(
    "/applications/{application_id}/prepare-delete",
    response_model=ApplicationDeleteOperationDTO,
)
def prepare_application_delete(
    application_id: int,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Prepare a frozen deletion preview; actual deletion still needs confirmation."""
    settings = get_settings()
    detail = repository.application_detail(
        settings.db_path, user_id, application_id, timezone_name=settings.timezone,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="application not found")
    try:
        result = prepare_application_delete_operation(
            settings.db_path,
            user_id,
            company=detail["company"],
            position=detail["position"],
            application_id=application_id,
        )
    except ApplicationDeleteOperationConflict as error:
        _raise_application_delete_error(error)
        raise AssertionError("unreachable")  # pragma: no cover
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="application not found")
    return result


@router.put(
    "/applications/{application_id}/stage",
    response_model=ApplicationDetailResponse,
)
def move_application_stage(
    application_id: int,
    payload: ApplicationStageMoveRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Move only the broad stage and preserve current_step."""
    settings = get_settings()
    try:
        result = repository.move_application_stage(
            settings.db_path,
            user_id,
            application_id,
            expected_revision=payload.expected_revision,
            stage=payload.stage,
            origin=payload.origin,
            timezone_name=settings.timezone,
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    return result


@router.put(
    "/applications/{application_id}/next-action",
    response_model=ApplicationDetailResponse,
)
def update_application_next_action(
    application_id: int,
    payload: ApplicationNextActionUpdateRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    settings = get_settings()
    try:
        result = repository.set_application_next_action(
            settings.db_path,
            user_id,
            application_id,
            expected_revision=payload.expected_revision,
            next_action=payload.next_action,
            timezone_name=settings.timezone,
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    return result


@router.post(
    "/applications/{application_id}/progress",
    response_model=ApplicationDetailResponse,
)
def record_application_progress(
    application_id: int,
    payload: ApplicationProgressWriteRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    settings = get_settings()
    try:
        result = repository.record_application_progress(
            settings.db_path,
            user_id,
            application_id,
            expected_revision=payload.expected_revision,
            step=payload.step,
            occurred_date=payload.occurred_date,
            outcome=payload.outcome,
            summary=payload.summary,
            update_current_state=payload.update_current_state,
            target_stage=payload.target_stage,
            target_step=payload.target_step,
            replace_next_action="next_action" in payload.model_fields_set,
            next_action=payload.next_action,
            timezone_name=settings.timezone,
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    return result


@router.post(
    "/applications/{application_id}/complete-next-action",
    response_model=ApplicationDetailResponse,
)
def complete_application_next_action(
    application_id: int,
    payload: CompleteNextActionRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    settings = get_settings()
    try:
        result = repository.complete_application_next_action(
            settings.db_path,
            user_id,
            application_id,
            expected_revision=payload.expected_revision,
            occurred_date=payload.occurred_date,
            outcome=payload.outcome,
            summary=payload.summary,
            next_action=payload.next_action,
            timezone_name=settings.timezone,
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    return result


@router.put(
    "/applications/{application_id}/timeline-entries/{entry_id}",
    response_model=ApplicationDetailResponse,
)
def update_timeline_entry(
    application_id: int,
    entry_id: int,
    payload: TimelineEntryUpdateRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    settings = get_settings()
    source = repository.timeline_entry_source(
        settings.db_path, user_id, application_id, entry_id,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="history entry not found")
    try:
        if source == "review":
            edited_entry = reviews.edit_review_timeline_entry_from_timeline(
                settings.db_path,
                user_id,
                application_id,
                entry_id,
                expected_revision=payload.expected_revision,
                expected_fingerprint=payload.expected_fingerprint,
                step=payload.step,
                occurred_date=payload.occurred_date,
                outcome=payload.outcome,
                summary=payload.summary,
            )
            if edited_entry is None:
                result = None
            else:
                result = repository.application_detail(
                    settings.db_path,
                    user_id,
                    application_id,
                    timezone_name=settings.timezone,
                )
        else:
            result = repository.update_timeline_entry(
                settings.db_path,
                user_id,
                application_id,
                entry_id,
                expected_revision=payload.expected_revision,
                expected_fingerprint=payload.expected_fingerprint,
                step=payload.step,
                occurred_date=payload.occurred_date,
                outcome=payload.outcome,
                summary=payload.summary,
                timezone_name=settings.timezone,
            )
    except (repository.TimelineMutationConflict,
            reviews.ReviewTimelineEntryEditOperationConflict) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="history entry not found")
    return result


@router.delete(
    "/applications/{application_id}/timeline-entries/{entry_id}",
    response_model=ApplicationDetailResponse,
)
def delete_timeline_entry(
    application_id: int,
    entry_id: int,
    expected_revision: int = Query(ge=0),
    expected_fingerprint: str = Query(pattern=r"^[0-9a-f]{64}$"),
    user_id: str = Depends(current_user_id),
) -> dict:
    settings = get_settings()
    source = repository.timeline_entry_source(
        settings.db_path, user_id, application_id, entry_id,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="history entry not found")
    if source == "review":
        raise HTTPException(
            status_code=409,
            detail="复盘历程必须通过完整撤销预览处理",
        )
    try:
        result = repository.delete_timeline_entry(
            settings.db_path,
            user_id,
            application_id,
            entry_id,
            expected_revision=expected_revision,
            expected_fingerprint=expected_fingerprint,
            timezone_name=settings.timezone,
        )
    except repository.TimelineMutationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="history entry not found")
    return result
