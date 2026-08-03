"""Test-only helpers for the trusted Review record operation boundary."""

from uuid import UUID, uuid4

from careerdesk.features.reviews.ai_models import ReviewExtraction
from careerdesk.features.reviews.operations.record import approve_review_record_operation
from careerdesk.features.reviews.repository import _derive_review_in_transaction
from careerdesk.platform.database import transaction


async def execute_review_record(
    service,
    user_id: str,
    text: str,
    *,
    today: str | None = None,
    review_reference: str | UUID | None = None,
    operation_id: str | UUID | None = None,
    client_turn_id: str | UUID | None = None,
    approve: bool = True,
) -> dict:
    """Execute one command and, by default, model the page's explicit approval click."""
    canonical_operation_id = operation_id or uuid4()
    operation = await service.execute_record_operation(
        user_id,
        operation_id=canonical_operation_id,
        client_turn_id=client_turn_id or uuid4(),
        text=text,
        review_reference=review_reference,
        today=today,
    )
    if approve and operation["state"] == "pending_confirmation":
        return approve_review_record_operation(
            service._db_path,
            user_id,
            canonical_operation_id,
        )
    return operation


def derive_review_for_test(
    db_path: str,
    user_id: str,
    journal_id: int,
    extraction: dict,
    *,
    replay: bool = False,
    expected_state: str = "pending",
    expected_revision: int = 0,
    reuse_current_application: bool = False,
    preserve_application_projection: bool = False,
    application_stage_transition: tuple[str, str] | None = None,
) -> dict:
    """Deterministically seed a derived Review for low-level repository tests.

    Production callers must use the two-phase Review record operation. Tests for
    undo/correction primitives still need a compact way to construct exact legacy
    business projections without invoking an external extractor.
    """
    canonical = ReviewExtraction.model_validate(extraction).model_dump(mode="json")
    with transaction(db_path) as conn:
        return _derive_review_in_transaction(
            conn,
            user_id,
            journal_id,
            canonical,
            replay=replay,
            expected_state=expected_state,
            expected_revision=expected_revision,
            reuse_current_application=reuse_current_application,
            preserve_application_projection=preserve_application_projection,
            application_stage_transition=application_stage_transition,
        )
