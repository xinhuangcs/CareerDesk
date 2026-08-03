"""Stable cross-domain read and orchestration entry point for Resumes."""

from .policy import STEADY_BOX, normalize_resume_line, resume_analysis_lines
from .repository import (
    get_resume,
    list_resumes,
    list_resume_summaries,
    pick_resume_for_application,
    resume_generation_snapshot_in_transaction,
    resume_adaptation_candidates_in_transaction,
)
from .service import ResumeService

__all__ = [
    "ResumeService",
    "STEADY_BOX",
    "get_resume",
    "list_resumes",
    "list_resume_summaries",
    "normalize_resume_line",
    "pick_resume_for_application",
    "resume_adaptation_candidates_in_transaction",
    "resume_analysis_lines",
    "resume_generation_snapshot_in_transaction",
]
