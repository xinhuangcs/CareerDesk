"""Stable preference entry point for agent tools and HTTP adapters."""

from .models import PreferenceOperationDTO
from .item_commands import (
    PreferenceItemCommandConflict,
    cancel_preference_item_command_if_absent,
    canonical_cancel_command,
    canonical_item_command,
    execute_preference_item_command,
    get_preference_item_command_status,
    get_preference_item_operation,
)
from .operations import (
    PreferenceOperationConflict,
    PreferenceOperationNotFound,
    PreferenceTurnAlreadyCommitted,
    canonical_apply_command,
    execute_preference_update_operation,
    get_preference_operation,
    list_preference_operations_for_turn,
)
from .repository import (
    PreferenceProjectionConflict,
    list_current_preferences,
    list_preferences_for_settings,
)

__all__ = [
    "PreferenceItemCommandConflict",
    "PreferenceOperationConflict",
    "PreferenceOperationDTO",
    "PreferenceOperationNotFound",
    "PreferenceTurnAlreadyCommitted",
    "PreferenceProjectionConflict",
    "cancel_preference_item_command_if_absent",
    "canonical_cancel_command",
    "canonical_item_command",
    "canonical_apply_command",
    "execute_preference_item_command",
    "execute_preference_update_operation",
    "get_preference_item_command_status",
    "get_preference_item_operation",
    "get_preference_operation",
    "list_current_preferences",
    "list_preferences_for_settings",
    "list_preference_operations_for_turn",
]
