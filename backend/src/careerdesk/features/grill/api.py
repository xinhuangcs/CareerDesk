"""REST endpoints for the immutable question-set Grill state machine."""

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from ...auth import current_user_id
from ...core.config import get_settings
from ...platform.ai.client import build_llm
from .contracts import GrillDeleteResponse, GrillFlowResponse, GrillReplayResponse, GrillSessionsResponse
from .service import GrillService

router = APIRouter(prefix="/api/grill")


def _service() -> GrillService:
    settings = get_settings()
    if settings.llm_model is None:
        return GrillService(settings.db_path, None)

    def load_llm():
        return build_llm(settings.llm_model, strict_offline=settings.strict_offline,
                         context_window=getattr(settings, "llm_context_window", None),
                         max_output_tokens=getattr(settings, "llm_max_output_tokens", None))

    return GrillService(settings.db_path, None, llm_configured=True, llm_factory=load_llm)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnswerRequest(_Request):
    session_id: int = Field(gt=0)
    session_item_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=20_000)
    answering_follow_up: bool = False

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("回答不能为空")
        return value.strip()


class SkipRequest(_Request):
    session_id: int = Field(gt=0)
    session_item_id: int = Field(gt=0)


class SessionRequest(_Request):
    session_id: int = Field(gt=0)


class ExperimentIntroClaimRequest(_Request):
    previously_seen: StrictBool = False


class ExperimentIntroClaimResponse(BaseModel):
    should_show: StrictBool
    release_version: str = Field(min_length=1, max_length=64)


@router.post("/experiment-intro/claim", response_model=ExperimentIntroClaimResponse)
def claim_experiment_intro(req: ExperimentIntroClaimRequest,
                           user_id: str = Depends(current_user_id)) -> dict:
    from . import repository
    should_show, release_version = repository.claim_experiment_intro(
        get_settings().db_path, user_id,
    )
    return {"should_show": should_show, "release_version": release_version}


@router.post("/answer", response_model=GrillFlowResponse, response_model_exclude_none=True)
async def answer(req: AnswerRequest, background: BackgroundTasks,
                 user_id: str = Depends(current_user_id)) -> dict:
    service = _service()
    result = await service.answer(user_id, req.session_id, req.text,
                                  session_item_id=req.session_item_id,
                                  answering_follow_up=req.answering_follow_up,
                                  enqueue_only=True)
    if result["status"] == "processing":
        async def finish() -> None:
            try:
                await service.run_pending_answer()
            finally:
                await service.close()

        background.add_task(finish)
    else:
        await service.close()
    return result


@router.post("/skip", response_model=GrillFlowResponse, response_model_exclude_none=True)
def skip(req: SkipRequest, user_id: str = Depends(current_user_id)) -> dict:
    return _service().skip(user_id, req.session_id, session_item_id=req.session_item_id)


@router.post("/suspend", response_model=GrillFlowResponse, response_model_exclude_none=True)
def suspend(req: SessionRequest, user_id: str = Depends(current_user_id)) -> dict:
    return _service().suspend(user_id, req.session_id)


@router.post("/resume", response_model=GrillFlowResponse, response_model_exclude_none=True)
def resume(req: SessionRequest, user_id: str = Depends(current_user_id)) -> dict:
    return _service().resume(user_id, req.session_id)


@router.get("/sessions", response_model=GrillSessionsResponse)
def list_sessions(state: str = "active,suspended", user_id: str = Depends(current_user_id)) -> dict:
    from . import repository
    states = [value.strip() for value in state.split(",") if value.strip()]
    return {"items": repository.list_sessions(get_settings().db_path, user_id, states)}


@router.get("/sessions/{session_id}/summary", response_model=GrillReplayResponse,
            response_model_exclude_none=True)
async def summary(session_id: int, user_id: str = Depends(current_user_id)) -> dict:
    return await _service().summary(user_id, session_id)


@router.post("/sessions/{session_id}/finalize", response_model=GrillReplayResponse,
             response_model_exclude_none=True)
async def finalize(session_id: int, user_id: str = Depends(current_user_id)) -> dict:
    return await _service().finalize_summary(user_id, session_id)


@router.delete("/sessions/{session_id}", response_model=GrillDeleteResponse,
               response_model_exclude_none=True)
def delete_session(session_id: int, user_id: str = Depends(current_user_id)) -> dict:
    from . import repository
    removed = repository.delete_session(get_settings().db_path, user_id, session_id)
    return {"status": "ok"} if removed else {"status": "error", "message": "场次不存在"}
