"""Maintenance HTTP response contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MaintenanceStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    derive_pending: bool
    pending_count: int = Field(ge=0)


class MaintenanceReconcileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "completed", "error"]
    reconciled: int = Field(ge=0)
    message: str | None = None
