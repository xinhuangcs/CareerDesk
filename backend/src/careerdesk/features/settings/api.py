"""Settings API for model and credential configuration; writes are local-only."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from ...auth import current_user_id
from ...platform.storage import location as storage_location
from . import service

router = APIRouter(prefix="/api/settings")

SettingKey = Annotated[str, Field(min_length=1, max_length=128)]
SettingValue = Annotated[str, Field(max_length=512)]
SettingRevision = Annotated[str, Field(min_length=16, max_length=128)]


class OutboundPolicy(BaseModel):
    """Instance-wide outbound permissions submitted as one complete snapshot."""

    model_config = ConfigDict(extra="forbid")

    strict_offline: StrictBool
    allow_conversation_embedding: StrictBool
    allow_web_research: StrictBool
    allow_deep_research: StrictBool
    allow_ddg_fallback: StrictBool


def _default_outbound_policy() -> OutboundPolicy:
    """Default only for omitted SettingsUpdate fields; fields_set distinguishes no-op."""
    return OutboundPolicy(
        strict_offline=False,
        allow_conversation_embedding=False,
        allow_web_research=False,
        allow_deep_research=False,
        allow_ddg_fallback=True,
    )


class ProviderInfo(BaseModel):
    """Nonsensitive metadata for a selectable model provider."""

    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    default_model: str | None
    key_vars: list[str]
    local: bool
    context_window: int | None
    max_output_tokens: int | None


class ModelCapabilities(BaseModel):
    """Trusted model capacity persisted atomically with its configured model."""

    model_config = ConfigDict(extra="forbid")

    context_window: StrictInt | None = Field(default=None, ge=1_024, le=2_147_483_647)
    max_output_tokens: StrictInt | None = Field(default=None, ge=256, le=2_147_483_647)

    @model_validator(mode="after")
    def validate_pair(self):
        if (self.context_window is None) != (self.max_output_tokens is None):
            raise ValueError("context window 与 max output tokens 必须同时填写或同时清除")
        if (
            self.context_window is not None
            and self.max_output_tokens is not None
            and self.max_output_tokens > self.context_window
        ):
            raise ValueError("max output tokens 不能大于 context window")
        return self


class ModelCapabilityState(ModelCapabilities):
    model_config = ConfigDict(extra="forbid")

    source: Literal["provider", "configured", "missing"] | None


class ModelCapabilityManagedState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_window: bool
    max_output_tokens: bool


class EnvironmentManagedState(BaseModel):
    """Names supplied by shell/container before dotenv loading, without values."""

    model_config = ConfigDict(extra="forbid")

    llm_model: bool
    llm_capabilities: ModelCapabilityManagedState
    keys: dict[str, bool]
    outbound_policy: dict[str, bool]


class CredentialStorageState(BaseModel):
    """Where editable credentials persist; never contains a credential value."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["system", "configuration_file", "server_environment"]
    available: bool
    label: str
    issue: str | None


class OpenAICompatibleEndpointState(BaseModel):
    """Disclosable compatible-endpoint contract that never echoes invalid source text."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["configured", "missing", "invalid"]
    url: str | None
    source: Literal["OPENAI_BASE_URL", "LLM_BASE_URL"] | None
    externally_managed: bool
    issue: str | None


class SettingsState(BaseModel):
    """Settings response that reports credential presence without secret values."""

    model_config = ConfigDict(extra="forbid")

    editable: bool
    llm_model: str | None
    llm_model_local: bool | None
    llm_capabilities: ModelCapabilityState
    keys: dict[str, bool]
    credential_storage: CredentialStorageState
    providers: list[ProviderInfo]
    outbound_policy: OutboundPolicy
    environment_managed: EnvironmentManagedState
    openai_compatible_endpoint: OpenAICompatibleEndpointState
    revision: str
    persistence_warning: str | None


class SettingsUpdate(BaseModel):
    """Partial update: omitted fields remain unchanged; null/empty clears values.

    Only submitted keys change. If outbound_policy is present, all booleans are required.
    """

    model_config = ConfigDict(extra="forbid")

    revision: SettingRevision
    llm_model: str | None = Field(default=None, max_length=512)
    llm_capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    keys: dict[SettingKey, SettingValue | None] = Field(default_factory=dict, max_length=64)
    outbound_policy: OutboundPolicy = Field(default_factory=_default_outbound_policy)


class StorageLocationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: str
    config_dir: str
    log_dir: str
    uses_default_data_dir: bool
    can_customize: bool
    customization_issue: str | None
    migration_pending: str | None
    migration_issue: str | None
    credential_storage_kind: Literal["system", "configuration_file", "server_environment"]
    credential_location: str


class StorageDisclosureClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    should_show: StrictBool


class StorageRevealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["data", "config", "logs"]


class StorageMigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str = Field(min_length=1, max_length=4_096)


class ConversationHistoryClearResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed"]
    deleted_messages: StrictInt = Field(ge=0)


class SystemTimezoneSyncRequest(BaseModel):
    """Browser-reported operating-system IANA timezone."""

    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(min_length=1, max_length=128)


class SystemTimezoneSyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["updated", "unchanged", "external_override"]
    timezone: str


@router.get("", response_model=SettingsState)
def get_state(user_id: str = Depends(current_user_id)) -> SettingsState:
    """Return model configuration and credential-presence flags, never key values."""
    return SettingsState.model_validate(service.read_state())


@router.put("", response_model=SettingsState)
def update_settings(payload: SettingsUpdate,
                          user_id: str = Depends(current_user_id)) -> SettingsState:
    """Save configuration and secure credentials, applying changes immediately."""
    if not service.ui_editable():
        raise HTTPException(status_code=403, detail="部署模式下配置由服务器 .env 管理，设置页不可用。")
    try:
        state = service.save(
            expected_revision=payload.revision,
            model_given="llm_model" in payload.model_fields_set,
            llm_model=payload.llm_model,
            capabilities_given="llm_capabilities" in payload.model_fields_set,
            llm_capabilities=(
                payload.llm_capabilities.model_dump()
                if "llm_capabilities" in payload.model_fields_set
                else None
            ),
            keys=payload.keys,
            outbound_policy=(
                payload.outbound_policy.model_dump()
                if "outbound_policy" in payload.model_fields_set
                else None
            ),
        )
        return SettingsState.model_validate(state)
    except service.SettingsRevisionConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/system-timezone", response_model=SystemTimezoneSyncResponse)
def sync_system_timezone(
    payload: SystemTimezoneSyncRequest,
    user_id: str = Depends(current_user_id),
) -> SystemTimezoneSyncResponse:
    """Follow the local device timezone without exposing server-wide mutation."""
    if not service.ui_editable():
        raise HTTPException(
            status_code=403,
            detail="部署模式下时区由服务器 APP_TIMEZONE 管理。",
        )
    try:
        return SystemTimezoneSyncResponse.model_validate(
            service.sync_system_timezone(payload.timezone)
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post(
    "/conversation-history/clear",
    response_model=ConversationHistoryClearResponse,
)
def clear_conversation_history(
    user_id: str = Depends(current_user_id),
) -> ConversationHistoryClearResponse:
    """Explicitly delete the user's conversations and every local retrieval index."""
    from ...agentic.memory import clear_conversation_history as clear_history
    from ...core.config import get_settings

    deleted = clear_history(get_settings().db_path, user_id=user_id)
    return ConversationHistoryClearResponse(
        status="completed",
        deleted_messages=deleted,
    )


@router.get("/storage", response_model=StorageLocationState)
def get_storage_location(user_id: str = Depends(current_user_id)) -> StorageLocationState:
    """Return local paths only to this local authenticated instance; never include secrets."""
    try:
        return StorageLocationState.model_validate(storage_location.storage_state())
    except storage_location.StorageLocationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/storage-disclosure/claim", response_model=StorageDisclosureClaimResponse)
def claim_storage_disclosure(
    user_id: str = Depends(current_user_id),
) -> StorageDisclosureClaimResponse:
    """Atomically grant the one-time local-storage disclosure for this data root."""
    return StorageDisclosureClaimResponse(
        should_show=service.claim_storage_disclosure(),
    )


@router.post("/storage/reveal", response_model=StorageLocationState)
def reveal_storage_location(
    payload: StorageRevealRequest,
    user_id: str = Depends(current_user_id),
) -> StorageLocationState:
    try:
        storage_location.reveal_directory(payload.target)
        return StorageLocationState.model_validate(storage_location.storage_state())
    except storage_location.StorageLocationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/storage/migration", response_model=StorageLocationState)
def request_storage_migration(
    payload: StorageMigrationRequest,
    user_id: str = Depends(current_user_id),
) -> StorageLocationState:
    """Stage a verified relocation that runs only after the desktop server stops."""
    try:
        return StorageLocationState.model_validate(
            storage_location.request_data_directory_migration(payload.destination)
        )
    except (storage_location.StorageLocationError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/storage/migration", response_model=StorageLocationState)
def cancel_storage_migration(
    user_id: str = Depends(current_user_id),
) -> StorageLocationState:
    """Cancel only the pending request; neither the source nor a restored copy is deleted."""
    try:
        return StorageLocationState.model_validate(storage_location.cancel_pending_migration())
    except storage_location.StorageLocationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
