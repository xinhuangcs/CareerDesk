"""HTTP interface for preferences, item commands, and trusted operations."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...auth import current_user_id
from ...core.config import get_settings
from ...platform.runtime.recovery_scope import derive_recovery_scope
from .item_command_models import (
    PreferenceItemCommandStatus,
    PreferenceItemOperationDTO,
)
from .item_commands import (
    PreferenceItemCommandConflict,
    cancel_preference_item_command_if_absent,
    canonical_cancel_command,
    canonical_item_command,
    execute_preference_item_command,
    get_preference_item_command_status,
    get_preference_item_operation,
)
from .models import PreferenceOperationDTO, PreferenceSettingsSnapshot
from .operations import (
    PreferenceOperationConflict,
    get_preference_operation,
    list_preference_operations_for_turn,
)
from .repository import PreferenceProjectionConflict, list_preferences_for_settings

router = APIRouter(prefix="/api/preferences")
_SETTINGS_RECOVERY_SCOPE_DOMAIN = b"careerdesk:preference-settings-recovery-scope:v1\0"


class PreferenceOperationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[PreferenceOperationDTO] = Field(max_length=1)


@router.get("")
def get_preferences(
    user_id: str = Depends(current_user_id),
) -> PreferenceSettingsSnapshot:
    payload = None
    conflict_detail = None
    try:
        db_path = get_settings().db_path
        payload = list_preferences_for_settings(
            db_path,
            user_id,
            recovery_scope=derive_recovery_scope(
                db_path,
                user_id,
                domain=_SETTINGS_RECOVERY_SCOPE_DOMAIN,
            ),
        )
    except PreferenceProjectionConflict as error:
        conflict_detail = str(error)
    if conflict_detail is not None:
        raise HTTPException(status_code=409, detail=conflict_detail)
    return PreferenceSettingsSnapshot.model_validate(payload)


def _item_command_conflict(error: PreferenceItemCommandConflict) -> None:
    raise HTTPException(status_code=409, detail=str(error)) from error


async def _private_json(request: Request) -> object:
    parsed = None
    succeeded = False
    try:
        parsed = await request.json()
        succeeded = True
    except (TypeError, ValueError):
        pass
    if not succeeded:
        # JSONDecodeError retains the raw body; construct the public error after except.
        raise HTTPException(status_code=422, detail="偏好逐项命令参数无效")
    return parsed


@router.put("/item-commands/{command_id}")
async def put_preference_item_command(
    command_id: UUID,
    response: Response,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> PreferenceItemCommandStatus:
    payload = await _private_json(request)
    try:
        command = canonical_item_command(payload)
    except ValueError:
        raise HTTPException(status_code=422, detail="偏好逐项命令参数无效") from None
    try:
        status, created = execute_preference_item_command(
            get_settings().db_path,
            user_id,
            command_id,
            command,
        )
    except PreferenceItemCommandConflict as error:
        _item_command_conflict(error)
        raise AssertionError("unreachable")  # pragma: no cover
    response.status_code = 201 if created else 200
    return PreferenceItemCommandStatus.model_validate(status)


@router.get("/item-commands/{command_id}")
def read_preference_item_command(
    command_id: UUID,
    user_id: str = Depends(current_user_id),
) -> PreferenceItemCommandStatus:
    try:
        status = get_preference_item_command_status(
            get_settings().db_path,
            user_id,
            command_id,
        )
    except PreferenceItemCommandConflict as error:
        _item_command_conflict(error)
        raise AssertionError("unreachable")  # pragma: no cover
    if status is None:
        raise HTTPException(status_code=404, detail="偏好逐项命令不存在")
    return PreferenceItemCommandStatus.model_validate(status)


@router.post("/item-commands/{command_id}/cancel-if-absent")
async def cancel_preference_item_command(
    command_id: UUID,
    response: Response,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> PreferenceItemCommandStatus:
    payload = await _private_json(request)
    try:
        command = canonical_cancel_command(payload)
    except ValueError:
        raise HTTPException(status_code=422, detail="偏好逐项取消参数无效") from None
    try:
        status, created = cancel_preference_item_command_if_absent(
            get_settings().db_path,
            user_id,
            command_id,
            command,
        )
    except PreferenceItemCommandConflict as error:
        _item_command_conflict(error)
        raise AssertionError("unreachable")  # pragma: no cover
    response.status_code = 201 if created else 200
    return PreferenceItemCommandStatus.model_validate(status)


@router.get("/item-operations/{operation_id}")
def read_preference_item_operation(
    operation_id: UUID,
    user_id: str = Depends(current_user_id),
) -> PreferenceItemOperationDTO:
    try:
        operation = get_preference_item_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except PreferenceItemCommandConflict as error:
        _item_command_conflict(error)
        raise AssertionError("unreachable")  # pragma: no cover
    if operation is None:
        raise HTTPException(status_code=404, detail="偏好逐项 operation 不存在")
    return PreferenceItemOperationDTO.model_validate(operation)


@router.get("/operations/by-client-turn/{client_turn_id}")
def get_preference_operations_by_client_turn(
    client_turn_id: UUID,
    user_id: str = Depends(current_user_id),
) -> PreferenceOperationsResponse:
    operations = None
    conflict_detail = None
    try:
        operations = list_preference_operations_for_turn(
            get_settings().db_path,
            user_id,
            client_turn_id,
        )
    except PreferenceOperationConflict as error:
        conflict_detail = str(error)
    if conflict_detail is not None:
        raise HTTPException(status_code=409, detail=conflict_detail)
    return PreferenceOperationsResponse(operations=operations)


@router.get("/operations/{operation_id}")
def read_preference_operation(
    operation_id: UUID,
    user_id: str = Depends(current_user_id),
) -> PreferenceOperationDTO:
    operation = None
    conflict_detail = None
    try:
        operation = get_preference_operation(
            get_settings().db_path,
            user_id,
            operation_id,
        )
    except PreferenceOperationConflict as error:
        conflict_detail = str(error)
    if conflict_detail is not None:
        raise HTTPException(status_code=409, detail=conflict_detail)
    if operation is None:
        raise HTTPException(status_code=404, detail="偏好 operation 不存在")
    return PreferenceOperationDTO.model_validate(operation)
