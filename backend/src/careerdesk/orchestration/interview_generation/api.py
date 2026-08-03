"""REST surface for readiness, generation and immutable question sets."""

from fastapi import APIRouter, BackgroundTasks, Depends

from ...auth import current_user_id
from ...core.config import get_settings
from ...features.applications import public as applications
from ...features.grill.contracts import GrillFlowResponse
from ...features.questions import public as questions
from ...features.resumes import public as resumes
from ...platform.ai.client import build_llm, close_llm_client
from ...platform.ai.providers import resolve_model_capabilities
from .contracts import (
    GenerateQuestionSetRequest, GenerationResponse, QuestionSetArchiveResponse,
    QuestionSetStatusResponse, ReadinessResponse,
    StartInterviewSessionRequest,
)
from .workflow import (
    InterviewGenerationWorkflow, current_policy_fingerprint, freeze_input, readiness,
    start_current_session,
)

router = APIRouter(prefix="/api/interview-generation")
grill_router = APIRouter(prefix="/api/grill")


@grill_router.post("/start", response_model=GrillFlowResponse, response_model_exclude_none=True)
def start_interview_session(req: StartInterviewSessionRequest,
                            user_id: str = Depends(current_user_id)) -> dict:
    try:
        session_id, question, total = start_current_session(
            get_settings().db_path, user_id, question_set_id=req.question_set_id,
            question_count=req.question_count,
        )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "session_id": session_id, "question": question,
            "progress": {"answered": 0, "total": total}}


def _with_currentness(db_path: str, user_id: str, items: list[dict]) -> list[dict]:
    """Derive current/stale at read time; never mutate immutable set provenance."""
    for item in items:
        if item["kind"] == "library_snapshot":
            item["currentness"] = "fixed"
            continue
        if item["state"] != "ready":
            item["currentness"] = "not_ready"
            continue
        if item["policy_fingerprint"] != current_policy_fingerprint():
            item["currentness"] = "legacy"
            continue
        try:
            frozen = freeze_input(
                db_path, user_id, edition=item["edition"], resume_id=item["resume_id"],
                application_id=item["application_id"],
            )
            item["currentness"] = (
                "current" if frozen.material_fingerprint == item["material_fingerprint"] else "stale"
            )
        except ValueError:
            item["currentness"] = "stale"
    return items


def _readiness_question_set(item: dict) -> dict:
    keys = (
        "id", "kind", "edition", "resume_id", "application_id",
        "state", "stage", "safe_error_code", "context_label", "archived_at",
        "question_count", "unpracticed_count", "created_time", "updated_time", "currentness",
        "content_locale",
    )
    return {key: item.get(key) for key in keys}


@router.get("/readiness", response_model=ReadinessResponse, response_model_exclude_none=True)
def get_readiness(edition: str | None = None, resume_id: int | None = None,
                  application_id: int | None = None,
                  user_id: str = Depends(current_user_id)) -> dict:
    settings = get_settings()
    resume_items = resumes.list_resume_summaries(settings.db_path, user_id)
    board = applications.board(settings.db_path, user_id)
    lightweight_columns = {
        stage: [{"id": item["id"], "company": item["company"], "position": item["position"]}
                for item in items]
        for stage, items in board["columns"].items()
    }
    set_items = _with_currentness(
        settings.db_path, user_id,
        [item for item in questions.list_question_sets(settings.db_path, user_id)
         if item["kind"] == "generated"],
    )
    result = {
        "resumes": [{key: item.get(key) for key in (
            "id", "name", "family", "binding", "application_id", "archived",
            "annotation_status", "character_count",
        )} for item in resume_items],
        "applications": {"columns": lightweight_columns, "total": board["total"]},
        "question_sets": [_readiness_question_set(item) for item in set_items],
        "model_configured": settings.llm_model is not None,
    }
    if edition is not None:
        context_window, max_output_tokens = resolve_model_capabilities(
            settings.llm_model,
            context_window=settings.llm_context_window,
            max_output_tokens=settings.llm_max_output_tokens,
        )
        result["selection"] = readiness(
            settings.db_path, user_id, edition=edition, resume_id=resume_id,
            application_id=application_id,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        )
    return result


@router.post("/question-sets", response_model=GenerationResponse, response_model_exclude_unset=True)
async def generate_question_set(req: GenerateQuestionSetRequest,
                                background: BackgroundTasks,
                                user_id: str = Depends(current_user_id)) -> dict:
    settings = get_settings()
    if settings.llm_model is None:
        return {"status": "error", "code": "model_required", "message": "请先配置模型"}
    llm = build_llm(settings.llm_model, strict_offline=settings.strict_offline,
                    context_window=getattr(settings, "llm_context_window", None),
                    max_output_tokens=getattr(settings, "llm_max_output_tokens", None))
    workflow = InterviewGenerationWorkflow(
        settings.db_path, llm, model_label=settings.llm_model,
        context_window=getattr(settings, "llm_context_window", None),
        max_output_tokens=getattr(settings, "llm_max_output_tokens", None),
    )
    result = await workflow.generate(
            user_id, edition=req.edition, resume_id=req.resume_id,
            application_id=req.application_id,
            client_command_id=str(req.client_command_id), refresh=req.refresh,
            output_locale=req.output_locale,
            enqueue_only=True,
        )
    if result["status"] == "processing":
        async def finish() -> None:
            try:
                await workflow.run_pending()
            finally:
                await close_llm_client(llm)

        background.add_task(finish)
    else:
        await close_llm_client(llm)
    return result


@router.get("/question-sets/{question_set_id}", response_model=QuestionSetStatusResponse,
            response_model_exclude_none=True)
def question_set_status(question_set_id: int,
                        user_id: str = Depends(current_user_id)) -> dict:
    result = questions.get_question_set(get_settings().db_path, user_id, question_set_id)
    if result is None:
        return {"status": "error", "code": "not_found"}
    return _readiness_question_set(
        _with_currentness(get_settings().db_path, user_id, [result])[0],
    )


@router.delete("/question-sets/{question_set_id}", response_model=QuestionSetArchiveResponse)
def delete_question_set(question_set_id: int,
                        user_id: str = Depends(current_user_id)) -> dict:
    result = questions.archive_or_delete_question_set(
        get_settings().db_path, user_id, question_set_id,
    )
    return {"status": result or "not_found"}
