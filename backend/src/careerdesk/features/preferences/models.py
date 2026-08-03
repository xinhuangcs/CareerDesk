"""Strict contracts for preferences, batch commands, and trusted operations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

PREFERENCE_UPDATE_CONTRACT_VERSION = 1
MAX_JSON_SAFE_INTEGER = 9_007_199_254_740_991
MAX_PREFERENCE_KEY_CHARS = 100
MAX_PREFERENCE_VALUE_CHARS = 2_000
MAX_PREFERENCE_CHANGES = 20
MAX_ACTIVE_PREFERENCES = 100
MAX_PREFERENCE_TOTAL_CHARS = 20_000
MAX_PERSISTED_PREFERENCE_JSON_CHARS = 100_000

PreferenceKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_PREFERENCE_KEY_CHARS,
    ),
]
PreferenceValue = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_PREFERENCE_VALUE_CHARS,
    ),
]
UUIDText = Annotated[
    str,
    StringConstraints(pattern=(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}$"
    )),
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def _canonical_iso_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("时间戳必须是 canonical ISO 8601") from None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
        or parsed.isoformat() != value
    ):
        raise ValueError("时间戳必须是 canonical UTC ISO 8601")
    return value


BoundedTimestamp = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_canonical_iso_timestamp),
]
PreferenceAction = Literal["set", "delete"]
PreferenceOutcome = Literal["created", "updated", "deleted", "unchanged", "missing"]


class PreferenceApplyChange(BaseModel):
    """Tool-supplied change whose value lives only for this transaction."""

    model_config = ConfigDict(extra="forbid", strict=True)

    op: PreferenceAction
    key: PreferenceKey
    value: PreferenceValue | None = None

    @model_validator(mode="after")
    def value_matches_action(self) -> "PreferenceApplyChange":
        if self.op == "set" and self.value is None:
            raise ValueError("set 变更必须提供 value")
        if self.op == "delete" and self.value is not None:
            raise ValueError("delete 变更不能提供 value")
        return self


class PreferenceApplyCommand(BaseModel):
    """Sole batch preference command within one agent turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    changes: list[PreferenceApplyChange] = Field(
        min_length=1,
        max_length=MAX_PREFERENCE_CHANGES,
    )

    @model_validator(mode="after")
    def keys_are_unique_and_sorted(self) -> "PreferenceApplyCommand":
        keys = [item.key for item in self.changes]
        if len(keys) != len(set(keys)):
            raise ValueError("同一批偏好变更的 key 必须唯一")
        if keys != sorted(keys):
            raise ValueError("偏好变更必须按规范 key 排序")
        return self


class PreferenceItem(BaseModel):
    """Public read model for active preferences."""

    model_config = ConfigDict(extra="forbid", strict=True)

    key: PreferenceKey
    value: PreferenceValue
    revision: int = Field(gt=0, le=MAX_JSON_SAFE_INTEGER)
    created_time: BoundedTimestamp
    updated_time: BoundedTimestamp


class PreferenceListDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[PreferenceItem] = Field(max_length=MAX_ACTIVE_PREFERENCES)
    total: int = Field(ge=0, le=MAX_ACTIVE_PREFERENCES)
    total_chars: int = Field(ge=0, le=MAX_PREFERENCE_TOTAL_CHARS)

    @model_validator(mode="after")
    def summary_matches_items(self) -> "PreferenceListDTO":
        if self.total != len(self.items):
            raise ValueError("偏好 total 与 items 不一致")
        if self.total_chars != sum(len(item.key) + len(item.value) for item in self.items):
            raise ValueError("偏好 total_chars 与 items 不一致")
        keys = [item.key for item in self.items]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("偏好 items 必须按唯一 key 排序")
        return self


class PreferenceSettingsItem(PreferenceItem):
    """Owner identity used for optimistic settings-page CAS."""

    id: int = Field(gt=0, le=MAX_JSON_SAFE_INTEGER)


class PreferenceSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[PreferenceSettingsItem] = Field(max_length=MAX_ACTIVE_PREFERENCES)
    total: int = Field(ge=0, le=MAX_ACTIVE_PREFERENCES)
    total_chars: int = Field(ge=0, le=MAX_PREFERENCE_TOTAL_CHARS)
    recovery_scope: Digest

    @model_validator(mode="after")
    def summary_matches_items(self) -> "PreferenceSettingsSnapshot":
        if self.total != len(self.items):
            raise ValueError("偏好 total 与 items 不一致")
        if self.total_chars != sum(
            len(item.key) + len(item.value) for item in self.items
        ):
            raise ValueError("偏好 total_chars 与 items 不一致")
        keys = [item.key for item in self.items]
        ids = [item.id for item in self.items]
        if (
            keys != sorted(keys)
            or len(keys) != len(set(keys))
            or len(ids) != len(set(ids))
        ):
            raise ValueError("偏好 items identity 无效")
        return self


class PreferenceOperationAction(BaseModel):
    """Persistable command skeleton that intentionally omits values."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: PreferenceAction
    key: PreferenceKey


class PreferenceOperationEffect(BaseModel):
    """Structural change result containing row identity/revision, never value."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: PreferenceAction
    key: PreferenceKey
    outcome: PreferenceOutcome
    before_id: int | None = Field(default=None, gt=0, le=MAX_JSON_SAFE_INTEGER)
    before_revision: int | None = Field(default=None, gt=0, le=MAX_JSON_SAFE_INTEGER)
    final_id: int | None = Field(default=None, gt=0, le=MAX_JSON_SAFE_INTEGER)
    final_revision: int | None = Field(default=None, gt=0, le=MAX_JSON_SAFE_INTEGER)

    @model_validator(mode="after")
    def identity_matches_outcome(self) -> "PreferenceOperationEffect":
        before = (self.before_id, self.before_revision)
        final = (self.final_id, self.final_revision)
        if self.outcome == "created":
            valid = self.action == "set" and before == (None, None) and (
                self.final_id is not None and self.final_revision == 1
            )
        elif self.outcome == "updated":
            valid = (
                self.action == "set"
                and self.before_id is not None
                and self.before_id == self.final_id
                and self.before_revision is not None
                and self.final_revision == self.before_revision + 1
            )
        elif self.outcome == "deleted":
            valid = (
                self.action == "delete"
                and self.before_id is not None
                and self.before_revision is not None
                and final == (None, None)
            )
        elif self.outcome == "unchanged":
            valid = (
                self.action == "set"
                and self.before_id is not None
                and self.before_revision is not None
                and self.final_revision is not None
                and before == final
            )
        else:
            valid = self.action == "delete" and before == final == (None, None)
        if not valid:
            raise ValueError("偏好 effect identity 与 outcome 不一致")
        return self


class PreferenceOperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    requested_count: int = Field(gt=0, le=MAX_PREFERENCE_CHANGES)
    changed_count: int = Field(gt=0, le=MAX_PREFERENCE_CHANGES)
    unchanged_count: int = Field(ge=0, le=MAX_PREFERENCE_CHANGES)
    created_count: int = Field(ge=0, le=MAX_PREFERENCE_CHANGES)
    updated_count: int = Field(ge=0, le=MAX_PREFERENCE_CHANGES)
    deleted_count: int = Field(ge=0, le=MAX_PREFERENCE_CHANGES)
    missing_count: int = Field(ge=0, le=MAX_PREFERENCE_CHANGES)

    @model_validator(mode="after")
    def changed_matches_outcomes(self) -> "PreferenceOperationResult":
        if self.changed_count != (
            self.created_count + self.updated_count + self.deleted_count
        ):
            raise ValueError("偏好 changed 计数不一致")
        if self.requested_count != (
            self.changed_count + self.unchanged_count + self.missing_count
        ):
            raise ValueError("偏好 requested 计数不一致")
        return self


class PreferenceOperationEnvelope(BaseModel):
    """Canonical terminal receipt duplicated across extraction and derivation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["preference_update"]
    contract_version: Literal[PREFERENCE_UPDATE_CONTRACT_VERSION]
    operation_id: UUIDText
    client_turn_id: UUIDText
    actions: list[PreferenceOperationAction] = Field(
        min_length=1,
        max_length=MAX_PREFERENCE_CHANGES,
    )
    effects: list[PreferenceOperationEffect] = Field(
        min_length=1,
        max_length=MAX_PREFERENCE_CHANGES,
    )
    result: PreferenceOperationResult
    structure_digest: Digest

    @model_validator(mode="after")
    def receipt_is_self_consistent(self) -> "PreferenceOperationEnvelope":
        action_pairs = [(item.action, item.key) for item in self.actions]
        effect_pairs = [(item.action, item.key) for item in self.effects]
        if action_pairs != effect_pairs:
            raise ValueError("偏好 actions 与 effects 不一致")
        keys = [item.key for item in self.actions]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("偏好 operation actions 必须按唯一 key 排序")
        counts = {name: 0 for name in (
            "created", "updated", "deleted", "unchanged", "missing",
        )}
        for effect in self.effects:
            counts[effect.outcome] += 1
        if any(
            getattr(self.result, f"{name}_count") != count
            for name, count in counts.items()
        ):
            raise ValueError("偏好 result 与 effects 计数不一致")
        if self.structure_digest != preference_structure_digest(self):
            raise ValueError("偏好 structure digest 不一致")
        return self


class PreferenceOperationEffectDTO(PreferenceOperationEffect):
    current: bool


class PreferenceOperationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["preference_update"]
    contract_version: Literal[PREFERENCE_UPDATE_CONTRACT_VERSION]
    state: Literal["completed"]
    operation_id: UUIDText
    client_turn_id: UUIDText
    created_time: BoundedTimestamp
    effects: list[PreferenceOperationEffectDTO] = Field(
        min_length=1,
        max_length=MAX_PREFERENCE_CHANGES,
    )
    current: bool
    result: PreferenceOperationResult

    @model_validator(mode="after")
    def current_matches_effects(self) -> "PreferenceOperationDTO":
        keys = [item.key for item in self.effects]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("偏好 DTO effects 必须按唯一 key 排序")
        counts = {name: 0 for name in (
            "created", "updated", "deleted", "unchanged", "missing",
        )}
        for effect in self.effects:
            counts[effect.outcome] += 1
        if any(
            getattr(self.result, f"{name}_count") != count
            for name, count in counts.items()
        ):
            raise ValueError("偏好 DTO result 与 effects 计数不一致")
        if self.current != all(item.current for item in self.effects):
            raise ValueError("偏好 operation current 与 effects 不一致")
        return self


def preference_structure_digest(envelope: PreferenceOperationEnvelope | dict) -> str:
    """Summarize structural receipts only; the input model has no preference values."""
    if isinstance(envelope, PreferenceOperationEnvelope):
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
