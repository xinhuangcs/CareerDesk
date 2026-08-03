"""Durable two-phase operations for recording a Review or one supplement.

Phase one durably stores the original source and a trusted operation claim.
The external extractor then runs without a SQLite transaction.  Phase three
revalidates the source, target revision and application ambiguity in one
``BEGIN IMMEDIATE`` transaction before atomically publishing Review projections
and the canonical operation receipt.
"""

from ..record_models import ReviewRecordProposal as ReviewRecordProposal
from .decisions import (
    approve_review_record_operation,
    decide_review_record_operations_for_turn,
    reject_review_record_operation,
    reject_review_record_operations_for_turn,
)
from .dto import _operation_rows_for_turn as _operation_rows_for_turn
from .errors import ReviewRecordOperationConflict, ReviewRecordOperationNotFound
from .execute import execute_review_record_operation
from .batch import execute_review_record_batch_operations
from .read import (
    get_review_record_operation,
    list_pending_review_record_clarifications,
    list_pending_review_record_confirmations,
    list_review_record_operations_for_turn,
    prepare_review_record_undo_operation,
    recover_interrupted_review_record_operations_in_transaction,
)
from .rows import (
    MAX_AMBIGUOUS_POSITION_OPTIONS as MAX_AMBIGUOUS_POSITION_OPTIONS,
    MAX_REVIEW_RECORD_PERSISTED_JSON_CHARS as MAX_REVIEW_RECORD_PERSISTED_JSON_CHARS,
    MAX_TURN_OPERATION_CANDIDATES as MAX_TURN_OPERATION_CANDIDATES,
    MAX_TURN_OPERATION_JSON_CHARS as MAX_TURN_OPERATION_JSON_CHARS,
    UNKNOWN_POSITION as UNKNOWN_POSITION,
    _bounded_combined as _bounded_combined,
    _canonical_json as _canonical_json,
    _proposal_digest as _proposal_digest,
    _supplement_rows as _supplement_rows,
    _text_digest as _text_digest,
)


__all__ = [
    "ReviewRecordOperationConflict",
    "ReviewRecordOperationNotFound",
    "approve_review_record_operation",
    "decide_review_record_operations_for_turn",
    "execute_review_record_operation",
    "execute_review_record_batch_operations",
    "get_review_record_operation",
    "list_pending_review_record_confirmations",
    "list_pending_review_record_clarifications",
    "list_review_record_operations_for_turn",
    "prepare_review_record_undo_operation",
    "reject_review_record_operation",
    "reject_review_record_operations_for_turn",
    "recover_interrupted_review_record_operations_in_transaction",
]
