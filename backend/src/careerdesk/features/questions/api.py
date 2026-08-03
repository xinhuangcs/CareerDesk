"""Question catalogue browsing and human verification endpoints."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from ...auth import current_user_id
from ...core.config import get_settings
from . import repository
from .contracts import CommandResponse, CompetencyProgressResponse, QuestionsResponse

router = APIRouter(prefix="/api/questions")


@router.get("/competency-progress", response_model=CompetencyProgressResponse)
def competency_progress(user_id: str = Depends(current_user_id)) -> dict:
    return repository.competency_overview(get_settings().db_path, user_id)


@router.get("", response_model=QuestionsResponse)
def browse_questions(
    edition: Literal["basic", "custom"] = Query(),
    knowledge_point: str | None = Query(default=None),
    category: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    question_set_id: int | None = Query(default=None, gt=0),
    order: str = Query(default="newest"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Browse immutable catalogue revisions with bounded pagination."""
    db_path = get_settings().db_path
    page = repository.list_questions(
        db_path, user_id, edition=edition, knowledge_point=knowledge_point,
        category=category, channel=channel, context_id=question_set_id,
        order=order, limit=limit + 1, offset=offset,
    )
    has_more = len(page) > limit
    return {
        "items": page[:limit],
        "weak_points": repository.list_weak_points(db_path, user_id, limit=30),
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


class QualityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flag: str | None


@router.put("/{question_id}/quality", response_model=CommandResponse)
def set_quality(question_id: int, req: QualityRequest,
                user_id: str = Depends(current_user_id)) -> dict:
    if req.flag not in ("good", "bad", None):
        return {"status": "error", "message": "flag 只能是 good / bad / null"}
    updated = repository.set_quality_flag(
        get_settings().db_path, user_id, question_id, req.flag,
    )
    return {"status": "ok"} if updated else {
        "status": "error", "message": f"找不到题目 #{question_id}",
    }


class VerifyGuideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified: bool


@router.put("/{question_id}/answer-guide-verification", response_model=CommandResponse)
def verify_guide(question_id: int, req: VerifyGuideRequest,
                 user_id: str = Depends(current_user_id)) -> dict:
    updated = repository.verify_answer_guide(
        get_settings().db_path, user_id, question_id, verified=req.verified,
    )
    return {"status": "ok"} if updated else {
        "status": "error", "message": f"题目 #{question_id} 没有可验证的回答指南",
    }
