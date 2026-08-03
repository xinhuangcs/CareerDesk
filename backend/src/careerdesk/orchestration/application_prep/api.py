"""Cross-domain research API retaining the existing Timeline URL contract."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from ...auth import current_user_id
from ...core.config import get_settings
from ...platform.locale import OutputLocale
from .briefing import compose_briefing
from .commands import PrepApplicationNotFound, request_prep_generation
from .http_contracts import (
    BriefingResponse,
    PrepTriggerResponse,
    ResumeAdaptationGenerateRequest,
    ResumeAdaptationInputPreviewResponse,
    ResumeAdaptationResponse,
)

router = APIRouter(prefix="/api/timeline")

@router.get(
    "/applications/{application_id}/briefing",
    response_model=BriefingResponse,
)
def get_briefing(
    application_id: int,
    locale: OutputLocale = Query(default="zh-CN"),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Return a code-only briefing assembled from persisted/precomputed data."""
    result = compose_briefing(
        get_settings().db_path, user_id, application_id, output_locale=locale
    )
    if result["status"] == "error":
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.get(
    "/applications/{application_id}/resume-adaptation",
    response_model=ResumeAdaptationResponse,
)
async def get_resume_adaptation(
    application_id: int,
    locale: OutputLocale = Query(default="zh-CN"),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Read local material and loop-owned task state without starting work."""
    from .factory import build_resume_adaptation_workflow

    result = build_resume_adaptation_workflow(
        get_settings(), output_locale=locale
    ).inspect(
        user_id,
        application_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    return result


@router.post(
    "/applications/{application_id}/resume-adaptation",
    response_model=ResumeAdaptationResponse,
)
async def generate_resume_adaptation(
    application_id: int,
    payload: ResumeAdaptationGenerateRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Run the controlled workflow; request flags authorize only this invocation."""
    from .factory import build_resume_adaptation_workflow

    result = await build_resume_adaptation_workflow(
        get_settings(), output_locale=payload.output_locale
    ).generate(
        user_id,
        application_id,
        refresh=payload.refresh,
        expected_resume_id=payload.expected_resume_id,
        accept_no_research=payload.accept_no_research,
        accept_summarized=payload.accept_summarized,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    return result


@router.get(
    "/applications/{application_id}/resume-adaptation/input-preview",
    response_model=ResumeAdaptationInputPreviewResponse,
)
def get_resume_adaptation_input_preview(
    application_id: int,
    locale: OutputLocale = Query(default="zh-CN"),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Preview the exact full-text or persisted-summary form used by the model."""
    from .factory import build_resume_adaptation_workflow

    result = build_resume_adaptation_workflow(
        get_settings(), output_locale=locale
    ).input_preview(
        user_id,
        application_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="application not found")
    if result.get("status") != "ok":
        raise HTTPException(status_code=409, detail="bind a resume before previewing model input")
    return {key: value for key, value in result.items() if key != "status"}


@router.post(
    "/applications/{application_id}/prep",
    response_model=PrepTriggerResponse,
    response_model_exclude_unset=True,
)
async def trigger_prep(application_id: int, background: BackgroundTasks,
                       force: bool = Query(default=False),
                       refresh_research: bool = Query(default=False),
                       output_locale: OutputLocale = Query(default="zh-CN"),
                       user_id: str = Depends(current_user_id)) -> dict:
    """Claim and queue research only after the explicit user action."""
    def schedule(service, owner_user_id, owner_application_id, **kwargs) -> None:
        background.add_task(service.run, owner_user_id, owner_application_id, **kwargs)

    try:
        return await request_prep_generation(
            get_settings(),
            user_id,
            application_id,
            force=force,
            refresh_research=refresh_research,
            schedule=schedule,
            output_locale=output_locale,
        )
    except PrepApplicationNotFound as error:
        raise HTTPException(status_code=404, detail="application not found") from error
