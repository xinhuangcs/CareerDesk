"""Stable cross-domain transactional entry point for Knowledge."""

from .repository import (
    link_question_knowledge_in_transaction,
    touch_knowledge_point_in_transaction,
)

__all__ = [
    "link_question_knowledge_in_transaction",
    "touch_knowledge_point_in_transaction",
]
