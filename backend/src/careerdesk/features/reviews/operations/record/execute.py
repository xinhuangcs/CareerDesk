"""Extractor invocation and the public execute entry point."""

from __future__ import annotations

import asyncio
import inspect
from uuid import UUID

from ...ai_models import ReviewExtraction
from .begin import _begin_operation
from .finalize import _fail_processing_operation, _stage_operation_for_confirmation
from .rows import (
    _canonical_uuid,
    _request_digest,
    _validate_source_text,
    _validate_user_id,
)


async def _extract(extractor, combined_text: str, effective_date: str) -> ReviewExtraction:
    produced = extractor(combined_text, effective_date)
    if inspect.isawaitable(produced):
        produced = await produced
    return ReviewExtraction.model_validate(produced)


async def execute_review_record_operation(
    db_path: str,
    user_id: str,
    *,
    operation_id: str | UUID,
    client_turn_id: str | UUID,
    text: str,
    review_reference: str | UUID | None,
    effective_date: str,
    extractor,
) -> dict:
    """Execute or replay one trusted Review intake/supplement operation."""
    _validate_user_id(user_id)
    canonical_operation = _canonical_uuid(operation_id, label="operation_id")
    canonical_turn = _canonical_uuid(client_turn_id, label="client_turn_id")
    source_text = _validate_source_text(text)
    canonical_reference = (
        None
        if review_reference is None
        else _canonical_uuid(review_reference, label="review_reference")
    )
    # Pydantic validates this again in the persisted proposal; validate before DB writes too.
    try:
        from datetime import date

        parsed_date = date.fromisoformat(effective_date)
    except (TypeError, ValueError) as error:
        raise ValueError("effective_date 必须是真实的 YYYY-MM-DD") from error
    if parsed_date.isoformat() != effective_date:
        raise ValueError("effective_date 必须是规范的 YYYY-MM-DD")
    if not callable(extractor):
        raise TypeError("extractor must be callable")
    request_digest = _request_digest(
        canonical_turn,
        source_text,
        canonical_reference,
    )
    claim = _begin_operation(
        db_path,
        user_id,
        operation_id=canonical_operation,
        client_turn_id=canonical_turn,
        text=source_text,
        review_reference=canonical_reference,
        effective_date=effective_date,
        request_digest=request_digest,
    )
    if not claim.execute:
        return claim.dto
    if claim.combined_text is None:  # pragma: no cover
        raise RuntimeError("record_review owner did not receive extractor input")
    try:
        extraction = await _extract(extractor, claim.combined_text, effective_date)
    except asyncio.CancelledError:
        _fail_processing_operation(
            db_path,
            user_id,
            claim.proposal,
            code="extract_cancelled",
            message="复盘提取被取消；原文已保留，但没有发布业务投影。",
        )
        raise
    except Exception:
        _fail_processing_operation(
            db_path,
            user_id,
            claim.proposal,
            code="extract_failed",
            message="复盘提取失败；原文已保留，但没有发布业务投影。",
        )
        raise
    try:
        return _stage_operation_for_confirmation(
            db_path,
            user_id,
            claim.proposal,
            extraction,
        )
    except Exception:
        # Preview publication failures roll back before this independent terminalization.
        # If commit actually completed, the helper observes and preserves its receipt.
        _fail_processing_operation(
            db_path,
            user_id,
            claim.proposal,
            code="publish_failed",
            message="复盘确认预览未能通过原子发布校验；原文已保留。",
        )
        raise
