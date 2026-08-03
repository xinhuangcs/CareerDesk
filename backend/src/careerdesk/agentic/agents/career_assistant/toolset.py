"""Sole assembly entry for assistant-visible tools; registry is request-scoped."""

from collections.abc import Callable
from sqlite3 import Connection
from uuid import UUID

from ....core.config import local_today
from ....features.applications.public import ApplicationService
from ....features.personal_state.public import build_personal_state_queries
from ....features.reviews.public import ReviewService
from ....platform.locale import OutputLocale
from ...runtime import CareerDeskToolRegistry, TrustedSkillCatalog
from ...tools import (ConversationSearchTool, DeleteApplicationTool, LoadSkillTool, ManageReviewTool, ParseJobsTool,
                      PreferencesTool, QueryGrillTool, QueryLibraryTool,
                      QueryPrepTool, QueryStatusTool, QueryStudyTool, QueryTimelineTool,
                      RecordReviewTool, RequestApplicationPrepTool, UpdateApplicationTool)
from ...tools.request_proposal_write_fence import RequestProposalWriteFence


def build_tool_registry(db_path: str, llm, user_id: str, catalog: TrustedSkillCatalog,
                        *, client_turn_id: str | UUID,
                        trusted_review_source: str,
                        review_supplement_reference: str | UUID | None = None,
                        proposal_recorder: Callable[[Connection, str, str], object] | None = None,
                        conversation=None,
                        output_locale: OutputLocale = "zh-CN") -> CareerDeskToolRegistry:
    """Build a user-bound native registry never shared across requests or users."""
    if not isinstance(trusted_review_source, str):
        raise TypeError("trusted_review_source must be a string")
    review_service = ReviewService(db_path, llm, output_locale=output_locale)
    application_service = ApplicationService(
        db_path,
        llm,
        proposal_recorder=proposal_recorder,
    )
    personal_state_queries = build_personal_state_queries(db_path)
    request_proposal_write_fence = RequestProposalWriteFence()
    tools = [
        LoadSkillTool(catalog, output_locale=output_locale),
        RecordReviewTool(review_service, user_id, client_turn_id=client_turn_id,
                         review_supplement_reference=review_supplement_reference,
                         trusted_source_text=trusted_review_source,
                         allow_batch=review_supplement_reference is None,
                         output_locale=output_locale,
                         request_proposal_write_fence=request_proposal_write_fence),
        ParseJobsTool(application_service, user_id,
                      request_proposal_write_fence=request_proposal_write_fence),
        UpdateApplicationTool(db_path, user_id, client_turn_id=client_turn_id,
                              local_date=local_today().isoformat(),
                              request_proposal_write_fence=request_proposal_write_fence,
                              proposal_recorder=proposal_recorder),
        DeleteApplicationTool(db_path, user_id,
                              request_proposal_write_fence=request_proposal_write_fence,
                              proposal_recorder=proposal_recorder),
        ManageReviewTool(db_path, user_id, client_turn_id=client_turn_id,
                         request_proposal_write_fence=request_proposal_write_fence,
                         proposal_recorder=proposal_recorder),
        QueryTimelineTool(db_path, user_id),
        QueryStudyTool(db_path, user_id),
        QueryLibraryTool(db_path, user_id),
        QueryGrillTool(db_path, user_id),
        QueryPrepTool(db_path, user_id),
        RequestApplicationPrepTool(user_id, output_locale=output_locale),
        QueryStatusTool(personal_state_queries, user_id),
        PreferencesTool(db_path, user_id, client_turn_id=client_turn_id),
    ]
    registry = CareerDeskToolRegistry(output_locale=output_locale)
    for tool in tools:
        if tool.origin != "careerdesk":
            raise ValueError(f"CareerDesk Tool has unexpected origin: {tool.name}={tool.origin}")
        registry.register(tool)

    if conversation is not None:
        registry.register(ConversationSearchTool(db_path, conversation, user_id))
    return registry
