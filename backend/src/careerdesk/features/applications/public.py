"""Stable cross-domain entry point for Applications."""

from collections.abc import Callable
import hashlib
import json

from ...platform.database import now_iso, read_connection, transaction

from .repository import (
    STAGE_LABELS,
    TimelineMutationConflict,
    apply_application_progress_in_transaction,
    append_application_note,
    application_detail,
    bind_application_resume,
    board,
    claim_prep_generation,
    fail_prep_generation,
    find_applications_by_company,
    freeze_resume_adaptation_input as _freeze_resume_adaptation_input,
    has_later_application_state_write_in_transaction,
    merge_prep_artifacts,
    merge_localized_prep_artifacts,
    merge_resume_adaptation_key_if_current as _merge_resume_adaptation_key_if_current,
    publish_research_snapshot,
    recover_interrupted_intakes,
    remove_review_created_application_in_transaction,
    resolve_application_by_name,
    restore_application_stage_in_transaction,
    set_application_note,
    set_prep_status,
    set_research_attempt,
    statistics,
    touch_prep_generation,
    timeline_entry_snapshot_fingerprint,
    upcoming,
)
from .repository.prep import _freeze_resume_adaptation_input_in_transaction
from .intake_models import ApplicationNextAction, ApplicationStage, StepText
from .service import ApplicationService
from .workbook_intake import (
    WORKBOOK_SUFFIXES,
    parse_standard_workbook,
    standard_positions_from_structured_text,
)
from .operations import (
    ApplicationDeleteOperationConflict,
    ApplicationMergeOperationConflict,
    has_completed_application_merge_lineage_in_transaction,
    ApplicationUpdateOperationConflict,
    ApplicationUpdateOperationNotFound,
    ApplicationUpdateOperationDTO,
    MAX_APPLICATION_UPDATE_BATCH_ITEMS,
    execute_application_update_batch,
    execute_application_update_operation,
    get_application_delete_operation,
    get_application_merge_operation,
    prepare_all_application_delete_operations,
    prepare_application_delete_operation,
    prepare_application_delete_operations,
    prepare_application_merge_operation,
    reject_application_delete_operation,
    reject_application_merge_operation,
)


def freeze_resume_adaptation_input(
    db_path: str,
    user_id: str,
    application_id: int,
) -> dict:
    """Freeze an adaptation aggregate through related features' public readers."""
    from ..companies.public import company_profile_in_transaction
    from ..resumes.public import resume_adaptation_candidates_in_transaction

    return _freeze_resume_adaptation_input(
        db_path,
        user_id,
        application_id,
        resume_reader=resume_adaptation_candidates_in_transaction,
        company_reader=company_profile_in_transaction,
    )


def freeze_resume_adaptation_input_in_transaction(
    conn,
    user_id: str,
    application_id: int,
) -> dict:
    """Freeze interview-generation inputs inside an orchestrator-owned snapshot."""
    from ..companies.public import company_profile_in_transaction
    from ..resumes.public import resume_adaptation_candidates_in_transaction

    return _freeze_resume_adaptation_input_in_transaction(
        conn,
        user_id,
        application_id,
        resume_reader=resume_adaptation_candidates_in_transaction,
        company_reader=company_profile_in_transaction,
    )


def merge_resume_adaptation_key_if_current(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    key: str,
    value: dict,
    expected_input_hash: str,
    current_validator: Callable[[dict, str], bool],
    content_locale: str | None = None,
) -> bool:
    """CAS-merge one task-owned key using the same cross-feature read snapshot."""
    from ..companies.public import company_profile_in_transaction
    from ..resumes.public import resume_adaptation_candidates_in_transaction

    return _merge_resume_adaptation_key_if_current(
        db_path,
        user_id,
        application_id,
        resume_reader=resume_adaptation_candidates_in_transaction,
        company_reader=company_profile_in_transaction,
        key=key,
        value=value,
        expected_input_hash=expected_input_hash,
        current_validator=current_validator,
        content_locale=content_locale,
    )


def confirm_application_jd(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    expected_content_hash: str | None = None,
) -> dict | None:
    """Confirm the exact current JD and bind a receipt to its content hash."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT jd_text FROM applications WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0].strip():
            return None
        digest = hashlib.sha256(row[0].encode("utf-8")).hexdigest()
        if expected_content_hash is not None and expected_content_hash != digest:
            raise ValueError("jd_content_changed")
        confirmed_time = now_iso()
        receipt = {
            "version": "jd-confirmation-v1",
            "content_hash": digest,
            "confirmed_time": confirmed_time,
            "source": "user_confirmed_current_text",
        }
        conn.execute(
            "UPDATE applications SET jd_content_hash = ?, jd_receipt_json = ?, "
            "jd_receipt_status = 'confirmed', revision = revision + 1, updated_time = ? "
            "WHERE user_id = ? AND id = ?",
            (digest, json.dumps(receipt, separators=(",", ":")), confirmed_time,
             user_id, application_id),
        )
    return receipt


def application_jd_confirmation(
    db_path: str,
    user_id: str,
    application_id: int,
) -> dict | None:
    """Return exact current JD text only for the explicit confirmation view."""
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT jd_text, jd_content_hash, jd_receipt_status FROM applications "
            "WHERE user_id = ? AND id = ?", (user_id, application_id),
        ).fetchone()
    if row is None:
        return None
    text = row[0]
    actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if isinstance(text, str) else None
    return {"jd_text": text, "content_hash": actual_hash,
            "confirmed": row[2] == "confirmed" and row[1] == actual_hash}


__all__ = [
    "ApplicationDeleteOperationConflict",
    "ApplicationMergeOperationConflict",
    "has_completed_application_merge_lineage_in_transaction",
    "ApplicationUpdateOperationConflict",
    "ApplicationUpdateOperationNotFound",
    "ApplicationUpdateOperationDTO",
    "ApplicationService",
    "ApplicationNextAction",
    "ApplicationStage",
    "STAGE_LABELS",
    "TimelineMutationConflict",
    "WORKBOOK_SUFFIXES",
    "StepText",
    "apply_application_progress_in_transaction",
    "append_application_note",
    "application_jd_confirmation",
    "application_detail",
    "bind_application_resume",
    "board",
    "claim_prep_generation",
    "confirm_application_jd",
    "fail_prep_generation",
    "find_applications_by_company",
    "freeze_resume_adaptation_input",
    "freeze_resume_adaptation_input_in_transaction",
    "has_later_application_state_write_in_transaction",
    "merge_prep_artifacts",
    "merge_localized_prep_artifacts",
    "merge_resume_adaptation_key_if_current",
    "publish_research_snapshot",
    "recover_interrupted_intakes",
    "remove_review_created_application_in_transaction",
    "prepare_all_application_delete_operations",
    "prepare_application_delete_operation",
    "prepare_application_delete_operations",
    "prepare_application_merge_operation",
    "parse_standard_workbook",
    "resolve_application_by_name",
    "restore_application_stage_in_transaction",
    "set_application_note",
    "MAX_APPLICATION_UPDATE_BATCH_ITEMS",
    "execute_application_update_batch",
    "execute_application_update_operation",
    "get_application_delete_operation",
    "get_application_merge_operation",
    "reject_application_delete_operation",
    "reject_application_merge_operation",
    "set_prep_status",
    "set_research_attempt",
    "statistics",
    "standard_positions_from_structured_text",
    "touch_prep_generation",
    "timeline_entry_snapshot_fingerprint",
    "upcoming",
]
