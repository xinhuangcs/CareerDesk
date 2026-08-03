"""Strict item preference commands and redacted operation receipts."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import (
    MAX_JSON_SAFE_INTEGER,
    BoundedTimestamp,
    Digest,
    PreferenceKey,
    PreferenceValue,
    UUIDText,
)

PREFERENCE_ITEM_COMMAND_CONTRACT_VERSION = 1
PREFERENCE_ITEM_OPERATION_CONTRACT_VERSION = 1

PreferenceItemCommandAction = Literal["set", "delete"]
PreferenceItemCommandOutcome = Literal["updated", "deleted", "no_change"]
PreferenceItemCommandErrorCode = Literal[
    "target_missing",
    "target_changed",
    "limit_exceeded",
    "projection_invalid",
]


class PreferenceItemTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0, le=MAX_JSON_SAFE_INTEGER)
    revision: int = Field(gt=0, le=MAX_JSON_SAFE_INTEGER)


class PreferenceItemCommandInput(BaseModel):
    """PUT input whose value lives only within this transaction."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: PreferenceItemCommandAction
    target: PreferenceItemTarget
    value: PreferenceValue | None = None

    @model_validator(mode="after")
    def value_matches_action(self) -> "PreferenceItemCommandInput":
        supplied = "value" in self.model_fields_set
        if self.action == "set" and (not supplied or self.value is None):
            raise ValueError("set 命令必须提供 value")
        if self.action == "delete" and supplied:
            raise ValueError("delete 命令不能提供 value")
        return self


class PreferenceItemCommandCancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: PreferenceItemCommandAction
    target: PreferenceItemTarget


class PreferenceItemCommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    outcome: PreferenceItemCommandOutcome
    before: PreferenceItemTarget
    final: PreferenceItemTarget | None

    @model_validator(mode="after")
    def result_shape(self) -> "PreferenceItemCommandResult":
        if self.outcome == "updated":
            valid = (
                self.final is not None
                and self.final.id == self.before.id
                and self.final.revision == self.before.revision + 1
            )
        elif self.outcome == "deleted":
            valid = self.final is None
        else:
            valid = self.final == self.before
        if not valid:
            raise ValueError("偏好逐项命令 result identity 无效")
        return self


class PreferenceItemCommandError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: PreferenceItemCommandErrorCode
    message: str = Field(min_length=1, max_length=256)


class PreferenceItemCommandStatus(BaseModel):
    """Complete terminal command resource shape; absence is HTTP 404."""

    model_config = ConfigDict(extra="forbid", strict=True)

    contract_version: Literal[PREFERENCE_ITEM_COMMAND_CONTRACT_VERSION]
    command_id: UUIDText
    state: Literal["completed", "rejected", "cancelled"]
    action: PreferenceItemCommandAction
    target: PreferenceItemTarget
    result: PreferenceItemCommandResult | None
    error: PreferenceItemCommandError | None
    operation_id: UUIDText | None
    finished_time: BoundedTimestamp

    @model_validator(mode="after")
    def terminal_shape(self) -> "PreferenceItemCommandStatus":
        if self.state == "completed":
            if self.result is None or self.error is not None:
                raise ValueError("completed command receipt 无效")
            changed = self.result.outcome in {"updated", "deleted"}
            if changed != (self.operation_id is not None):
                raise ValueError("completed command operation identity 无效")
            if self.result.before != self.target:
                raise ValueError("completed command target 无效")
            if (
                (self.action == "set")
                != (self.result.outcome in {"updated", "no_change"})
            ):
                raise ValueError("completed command action/outcome 无效")
        elif self.state == "rejected":
            if (
                self.result is not None
                or self.error is None
                or self.operation_id is not None
            ):
                raise ValueError("rejected command receipt 无效")
        elif any(item is not None for item in (
            self.result, self.error, self.operation_id,
        )):
            raise ValueError("cancelled command receipt 无效")
        return self


class PreferenceItemOperationEnvelope(BaseModel):
    """Changed-only journal envelope intentionally omitting values and summaries."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["preference_item_change"]
    contract_version: Literal[PREFERENCE_ITEM_OPERATION_CONTRACT_VERSION]
    operation_id: UUIDText
    command_id: UUIDText
    action: PreferenceItemCommandAction
    key: PreferenceKey
    result: PreferenceItemCommandResult
    structure_digest: Digest

    @model_validator(mode="after")
    def envelope_shape(self) -> "PreferenceItemOperationEnvelope":
        if self.result.outcome not in {"updated", "deleted"}:
            raise ValueError("item operation 只能记录真实变化")
        if (
            (self.action == "set") != (self.result.outcome == "updated")
            or self.structure_digest != preference_item_structure_digest(self)
        ):
            raise ValueError("item operation receipt 无效")
        return self


class PreferenceItemOperationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["preference_item_change"]
    contract_version: Literal[PREFERENCE_ITEM_OPERATION_CONTRACT_VERSION]
    state: Literal["completed"]
    operation_id: UUIDText
    command_id: UUIDText
    action: PreferenceItemCommandAction
    key: PreferenceKey
    result: PreferenceItemCommandResult
    created_time: BoundedTimestamp
    current: bool


def preference_item_structure_digest(
    envelope: PreferenceItemOperationEnvelope | dict,
) -> str:
    if isinstance(envelope, PreferenceItemOperationEnvelope):
        payload = envelope.model_dump(exclude={"structure_digest"})
    else:
        payload = dict(envelope)
        payload.pop("structure_digest", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
