"""Application Prep dependency assembly; service.py owns the state machine."""

from ...platform.ai.client import build_llm
from ...features.research.public import ResearchService, build_search
from .service import PrepService
from .adaptation_workflow import ResumeAdaptationWorkflow


def build_prep_llm(settings):
    """Construct one request-scoped model client before generation claim."""
    return build_llm(
        settings.llm_model,
        strict_offline=settings.strict_offline,
        context_window=getattr(settings, "llm_context_window", None),
        max_output_tokens=getattr(settings, "llm_max_output_tokens", None),
    )


def build_prep_service(settings, *, llm) -> PrepService:
    """Assemble the executor with a preflighted client to avoid post-claim rebuild."""
    pool = build_search(
        enabled=settings.web_research_enabled,
        deep=settings.deep_research_enabled,
        ddg_fallback=settings.allow_ddg_fallback,
    )
    return PrepService(
        settings.db_path,
        ResearchService(settings.db_path, llm, pool),
        llm=llm,
    )


def build_resume_adaptation_workflow(
    settings,
    *,
    db_path: str | None = None,
    output_locale="zh-CN",
) -> ResumeAdaptationWorkflow:
    """Build the local workflow shell; its factory constructs a client only in POST work."""
    llm_factory = None
    if settings.llm_model is not None:
        def llm_factory():
            return build_llm(
                settings.llm_model,
                strict_offline=settings.strict_offline,
                context_window=getattr(settings, "llm_context_window", None),
                max_output_tokens=getattr(settings, "llm_max_output_tokens", None),
            )
    return ResumeAdaptationWorkflow(
        db_path or settings.db_path,
        model_string=settings.llm_model,
        strict_offline=settings.strict_offline,
        context_window=getattr(settings, "llm_context_window", None),
        max_output_tokens=getattr(settings, "llm_max_output_tokens", None),
        llm_factory=llm_factory,
        output_locale=output_locale,
    )
