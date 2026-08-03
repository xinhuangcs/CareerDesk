"""Stable cross-domain entry point for Research."""

from .ai_tasks import ResearchAITaskError
from .contracts import (
    ResearchAttempt,
    ResearchSnapshot,
    build_research_snapshot,
    company_cache_eligibility_hash,
    derive_research_artifact_state,
    research_semantic_claim,
    research_semantic_claim_hash,
)
from .repository import get_company_profile, get_research_cache
from .queries import derive_search_profile
from .service import ResearchService, build_search, research_is_fresh

__all__ = [
    "ResearchAITaskError",
    "ResearchAttempt",
    "ResearchService",
    "ResearchSnapshot",
    "build_research_snapshot",
    "build_search",
    "company_cache_eligibility_hash",
    "derive_research_artifact_state",
    "derive_search_profile",
    "get_company_profile",
    "get_research_cache",
    "research_semantic_claim",
    "research_semantic_claim_hash",
    "research_is_fresh",
]
