"""Stable agent-facing entry point for Application Prep."""

from .commands import PrepApplicationNotFound, request_prep_generation


def compose_briefing(*args, **kwargs):
    """Lazy-load the code-only briefing to preserve read-only tool cost."""
    from .briefing import compose_briefing as compose

    return compose(*args, **kwargs)


def inspect_resume_adaptation(
    db_path: str,
    user_id: str,
    application_id: int,
) -> dict | None:
    """Read exact local adaptation state without constructing a provider client."""
    from ...core.config import get_settings
    from .factory import build_resume_adaptation_workflow

    return build_resume_adaptation_workflow(
        get_settings(), db_path=db_path,
    ).inspect(user_id, application_id)


__all__ = [
    "PrepApplicationNotFound",
    "compose_briefing",
    "inspect_resume_adaptation",
    "request_prep_generation",
]
