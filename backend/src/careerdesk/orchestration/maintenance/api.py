"""Maintenance API for explicitly confirmed, snapshot-verified cache work."""

from fastapi import APIRouter, Depends
from ...auth import current_user_id
from ...core.config import get_settings
from .service import MaintenanceService, upgrade_status
from .contracts import MaintenanceReconcileResponse, MaintenanceStatusResponse

router = APIRouter(prefix="/api/maintenance")


@router.get("/status", response_model=MaintenanceStatusResponse)
def status(user_id: str = Depends(current_user_id)) -> dict:
    return upgrade_status(get_settings().db_path, user_id)


@router.post(
    "/reconcile",
    response_model=MaintenanceReconcileResponse,
    response_model_exclude_unset=True,
)
async def reconcile(user_id: str = Depends(current_user_id)) -> dict:
    return await MaintenanceService(get_settings().db_path).reconcile(user_id)
