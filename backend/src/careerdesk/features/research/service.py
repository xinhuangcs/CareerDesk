"""Company and role research orchestration from planning through gap fill.

Company reports use a 14-day company-level cache shared across roles, while role
reports regenerate per preparation run. Providers, fetching, and material
processing live in dedicated modules; this module orchestrates and stores.
"""

import asyncio
import logging
import weakref
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ...core.config import get_settings, local_today
from ...platform.locale import OutputLocale
from ..applications import public as applications
from . import repository
from .ai_models import NOT_FOUND, NOT_FOUND_EN
from .contracts import company_cache_eligibility_hash, research_semantic_claim
from .ai_tasks import (
    ResearchAITaskError,
    compose_company_report,
    compose_position_report,
    compose_research_plan,
)
from .fetcher import PageFetcher
from .materials import (
    build_materials,
    bucket_leg_materials,
    materials_payload,
    select_fetch_urls,
    sources_metadata,
)
from .providers import build_provider_pool
from .queries import (
    PlannedQuery,
    gap_fill_queries,
    derive_search_profile,
    normalize_planned_queries,
    skeleton_plan,
)

logger = logging.getLogger(__name__)

RESEARCH_TTL_DAYS = 14
COMPANY_REPORT_VERSION = 3
COMPANY_REPORT_SECTIONS = ("business", "culture", "recent_news", "interview_style")
POSITION_REPORT_SECTIONS = ("interview_process", "experience_highlights", "team_and_work_context")
FETCH_PAGE_LIMIT = 8
GAP_FETCH_LIMIT = 4
JD_EXCERPT_CHARS = 2_000
PRESEARCH_MATERIAL_LIMIT = 10

# Production is single-worker. Coalesce research by event loop and database/user/
# company so roles for one company do not duplicate retrieval and composition.
# Weak loop keys release locks after TestClient/asyncio.run exits.
_RESEARCH_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _research_lock(db_path: str, user_id: str, company: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    # A contended Lock retains its event loop, so inner values must be weak or the
    # outer WeakKeyDictionary forms a cycle around short-lived CLI/test loops.
    locks = _RESEARCH_LOCKS.setdefault(loop, weakref.WeakValueDictionary())
    key = (str(Path(db_path).resolve()), user_id, company)
    return locks.setdefault(key, asyncio.Lock())


def research_is_fresh(
    research_time: str | None,
    today: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a research report remains within its exact TTL.

    ``today`` is the business/replay date. Project wall-clock time onto it while
    retaining time of day and app timezone, then compare exact duration so
    historical tests and replays do not depend on the machine's real date.
    """
    if not research_time:
        return False
    try:
        stored = datetime.fromisoformat(research_time)
    except ValueError:
        return False
    # Research time is UTC while today uses app-local time. Projection changes
    # only explicit replays; during normal operation today is already local today.
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now 必须包含时区")
    business_date = date.fromisoformat(today)
    zone = ZoneInfo(get_settings().timezone)
    local_current = current.astimezone(zone)
    effective_current = local_current.replace(
        year=business_date.year,
        month=business_date.month,
        day=business_date.day,
    )
    age = effective_current.astimezone(timezone.utc) - stored.astimezone(timezone.utc)
    return timedelta(0) <= age <= timedelta(days=RESEARCH_TTL_DAYS)


def _generated_time_for_today(today: str) -> str:
    """Use one business clock for explicit-date replays and cache writes."""
    target = date.fromisoformat(today)
    local_now = datetime.now(ZoneInfo(get_settings().timezone))
    local_generated = datetime(
        target.year,
        target.month,
        target.day,
        local_now.hour,
        local_now.minute,
        local_now.second,
        local_now.microsecond,
        tzinfo=ZoneInfo(get_settings().timezone),
    )
    return local_generated.astimezone(timezone.utc).isoformat()


def build_search(*, enabled: bool, deep: bool = False, ddg_fallback: bool = True):
    """Build configured search providers, or none when web research is disabled."""
    if not enabled:
        return None
    return build_provider_pool(enabled=True, deep=deep, ddg_fallback=ddg_fallback)


def _cited_section_dict(section, *, material_count: int) -> dict:
    """Store a structured section after dropping forged source indices."""
    return {
        "text": section.text,
        "sources": [index for index in section.sources if 1 <= index <= material_count],
    }


def _source_conflict_dicts(conflicts, *, material_count: int) -> list[dict]:
    """Keep only conflicts whose two or more distinct evidence refs resolve."""
    result: list[dict] = []
    for conflict in conflicts:
        sources = list(dict.fromkeys(
            index for index in conflict.sources if 1 <= index <= material_count
        ))
        if len(sources) < 2:
            continue
        result.append({"summary": conflict.summary, "sources": sources})
    return result


def _section_missing(section_dict: dict) -> bool:
    text = (section_dict or {}).get("text") or ""
    return NOT_FOUND in text or NOT_FOUND_EN in text or not text.strip()


class ResearchService:
    """Complete company and role research orchestration entry point."""

    def __init__(self, db_path: str, llm, search, *, fetcher=None):
        """Initialize with a provider pool and an optional injectable fetcher.

        ``search=None`` means web research is not authorized. A fetcher is created
        automatically only when search is available.
        """
        self._db_path = db_path
        self._llm = llm
        self._pool = search
        self._fetcher = fetcher if fetcher is not None else (
            PageFetcher() if search is not None else None
        )

    async def research(
        self,
        user_id: str,
        company: str,
        position: str,
        *,
        jd_text: str | None = None,
        department: str | None = None,
        refresh: bool = False,
        today: str | None = None,
        application_id: int | None = None,
        generation: str | None = None,
        output_locale: OutputLocale = "zh-CN",
    ) -> dict:
        """Run full research, skipping the company leg on a valid cache hit.

        Status is ``ok``, ``disabled``, ``unavailable``, or ``stale``. Success
        includes reports, anchor, planner, web-question candidates, and sources.
        """
        if (application_id is None) != (generation is None):
            raise ValueError("application_id 与 generation 必须同时提供")
        today = today or local_today().isoformat()

        def keep_running() -> bool:
            if application_id is None or generation is None:
                return True
            return applications.touch_prep_generation(
                self._db_path, user_id, application_id, generation, company=company)

        def stale() -> dict:
            return {"status": "stale", "company_report": None, "position_report": None}

        if not keep_running():
            return stale()
        # Freeze semantic input once. Cache/save and snapshot publication recheck
        # it so work made stale by an edit cannot enter a newer revision.
        company_profile = repository.get_company_profile(self._db_path, user_id, company)
        search_profile = derive_search_profile(company, position, jd_text)
        company_search_profile = derive_search_profile(company, "")
        cache_eligibility = company_cache_eligibility_hash(
            company=company,
            aliases=company_profile["aliases"],
            notes=company_profile["notes"],
            output_locale=output_locale,
            search_profile=company_search_profile,
        )
        semantic_claim = research_semantic_claim(
            company=company,
            aliases=company_profile["aliases"],
            notes=company_profile["notes"],
            department=department,
            position=position,
            jd_text=jd_text,
            output_locale=output_locale,
            search_profile=search_profile,
        )
        cached_before = repository.get_research_cache(
            self._db_path, user_id, company, output_locale=output_locale
        )
        cached_report = cached_before["research"] if cached_before else None
        baseline_time = cached_before["research_time"] if cached_before else None
        if self._pool is None:
            # Web research has independent consent. Disabled mode may read an old
            # cache but never searches or asks an LLM to dress up empty data.
            return self._degraded("disabled", cached_report)
        if not self._pool.has_outlets:
            return self._degraded("unavailable", cached_report)

        async with _research_lock(self._db_path, user_id, company):
            if not keep_running():
                return stale()
            # Reread after locking because a peer may have created a reusable
            # version. Refresh also reuses a version changed while waiting.
            cached = repository.get_research_cache(
                self._db_path, user_id, company, output_locale=output_locale
            )
            cached_report = cached["research"] if cached else None
            refreshed_by_peer = bool(cached and cached["research_time"] != baseline_time)
            company_cached = bool(
                cached_report
                and isinstance(cached_report, dict)
                and cached_report.get("version") == COMPANY_REPORT_VERSION
                and cached.get("eligibility_hash") == cache_eligibility
                and research_is_fresh(cached["research_time"], today)
                and (not refresh or refreshed_by_peer)
            )
            try:
                return await self._run_research(
                    user_id, company, position,
                    jd_text=jd_text, department=department, today=today,
                    application_id=application_id, generation=generation,
                    company_cached=company_cached, cached_report=cached_report,
                    company_profile=company_profile,
                    cache_eligibility=cache_eligibility,
                    semantic_claim=semantic_claim,
                    output_locale=output_locale,
                    search_profile=search_profile,
                    company_search_profile=company_search_profile,
                    keep_running=keep_running, stale=stale,
                )
            except ResearchAITaskError:
                raise
            except Exception:  # noqa: BLE001 -- retrieval must not break all prep
                logger.exception("research pipeline failed")
                return self._degraded("unavailable", cached_report)

    def _degraded(self, status: str, cached_report) -> dict:
        return {
            "status": status,
            "company_report": cached_report,
            "company_from_cache": cached_report is not None,
            "position_report": None,
            "anchor": (cached_report or {}).get("anchor")
            if isinstance(cached_report, dict) else None,
            "planner": None,
            "web_question_candidates": [],
        }

    async def _fetch_pages(self, outcomes, limit: int) -> dict:
        if self._fetcher is None:
            return {}
        return await self._fetcher.fetch_pages(select_fetch_urls(outcomes, limit=limit))

    async def _plan(self, company: str, position: str, *,
                    jd_text: str | None, department: str | None,
                    company_profile: dict, anchor_prior: dict | None,
                    keep_running, output_locale: OutputLocale,
                    search_profile: dict) -> tuple | None:
        """Anchor and plan retrieval, falling back to fixed query skeletons.

        None means the task became stale during pre-search, avoiding a planning
        call that no longer has a consumer.
        """
        presearch = await self._pool.run_plan([
            PlannedQuery(
                text=company,
                leg="company",
                section="business",
                key=True,
                language=search_profile["primary_language"],
                country=search_profile.get("country"),
            ),
        ])
        if not keep_running():
            return None
        presearch_materials = [
            {"title": hit.title, "url": hit.url, "snippet": hit.snippet}
            for outcome in presearch for hit in outcome.hits
        ][:PRESEARCH_MATERIAL_LIMIT]
        profile = company_profile
        if anchor_prior:
            profile = {**profile, "previous_anchor": anchor_prior}
        try:
            plan_model = await compose_research_plan(
                self._llm,
                company=company,
                position=position,
                jd_excerpt=(jd_text or "")[:JD_EXCERPT_CHARS],
                department=department,
                profile=profile,
                presearch_materials=presearch_materials,
                output_locale=output_locale,
            )
            raw_queries = [
                PlannedQuery(text=item.text, leg=item.leg, section=item.section,
                             kind=item.kind, key=item.key, language=item.language)
                for item in plan_model.queries
            ]
            plan = normalize_planned_queries(
                raw_queries,
                company=company,
                position=position,
                search_profile=search_profile,
            )
            anchor = plan_model.anchor.model_dump()
        except ResearchAITaskError:
            plan = skeleton_plan(company, position, search_profile=search_profile)
            anchor = anchor_prior or {
                "official_name": company, "website_domain": "", "industry": "",
                "location": "", "confidence": "low",
                "note": (
                    "Research planning was unavailable; bounded fallback queries were used."
                    if output_locale == "en"
                    else "检索规划暂不可用，本次使用固定骨架查询"
                ),
            }
        return plan, anchor

    async def _run_research(self, user_id: str, company: str, position: str, *,
                            jd_text: str | None, department: str | None, today: str,
                            application_id: int | None, generation: str | None,
                            company_cached: bool, cached_report,
                            company_profile: dict, cache_eligibility: str,
                            semantic_claim: dict,
                            output_locale: OutputLocale, search_profile: dict,
                            company_search_profile: dict,
                            keep_running, stale) -> dict:
        anchor_prior = (cached_report or {}).get("anchor") \
            if isinstance(cached_report, dict) else None
        planned = await self._plan(
            company, position, jd_text=jd_text, department=department,
            company_profile=company_profile, anchor_prior=anchor_prior,
            keep_running=keep_running,
            output_locale=output_locale,
            search_profile=search_profile,
        )
        if planned is None or not keep_running():
            return stale()
        plan, anchor = planned
        queries = plan.position_queries() if company_cached else plan.queries
        outcomes = await self._pool.run_plan(queries)
        if not keep_running():
            return stale()
        if outcomes and all(
                (not outcome.hits) and outcome.failed_engines for outcome in outcomes):
            # Total provider failure differs from a valid zero-hit result. Preserve
            # the old cache so remaining preparation stages can continue.
            return self._degraded("unavailable", cached_report)
        pages = await self._fetch_pages(outcomes, FETCH_PAGE_LIMIT)
        materials = build_materials(outcomes, pages)
        anchor_domain = (anchor or {}).get("website_domain") or None

        company_selected: list = []
        company_model = None
        if not company_cached:
            company_selected, _dropped = bucket_leg_materials(
                materials, leg="company", anchor_domain=anchor_domain, today=today)
            company_model = await compose_company_report(
                self._llm, company=company, anchor=anchor,
                materials=materials_payload(company_selected),
                output_locale=output_locale)
            if not keep_running():
                return stale()
        position_selected, _dropped = bucket_leg_materials(
            materials, leg="position", anchor_domain=anchor_domain, today=today)
        position_model = await compose_position_report(
            self._llm, company=company, position=position, anchor=anchor,
            materials=materials_payload(position_selected),
            output_locale=output_locale)
        if not keep_running():
            return stale()

        missing_company = [] if company_model is None else [
            section for section in COMPANY_REPORT_SECTIONS
            if _section_missing({"text": getattr(company_model, section).text})
        ]
        missing_position = [
            section for section in POSITION_REPORT_SECTIONS
            if _section_missing({"text": getattr(position_model, section).text})
        ]
        if missing_company or missing_position:
            gap_queries = gap_fill_queries(
                company, position,
                missing_company_sections=missing_company,
                missing_position_sections=missing_position,
                search_profile=search_profile)
            gap_outcomes = await self._pool.run_plan(gap_queries)
            pages.update(await self._fetch_pages(gap_outcomes, GAP_FETCH_LIMIT))
            outcomes = list(outcomes) + list(gap_outcomes)
            materials = build_materials(outcomes, pages)
            if missing_company:
                company_selected, _dropped = bucket_leg_materials(
                    materials, leg="company", anchor_domain=anchor_domain, today=today)
                company_model = await compose_company_report(
                    self._llm, company=company, anchor=anchor,
                    materials=materials_payload(company_selected),
                    output_locale=output_locale)
            if missing_position:
                position_selected, _dropped = bucket_leg_materials(
                    materials, leg="position", anchor_domain=anchor_domain, today=today)
                position_model = await compose_position_report(
                    self._llm, company=company, position=position, anchor=anchor,
                    materials=materials_payload(position_selected),
                    output_locale=output_locale)
            if not keep_running():
                return stale()

        if company_model is not None:
            company_report = {
                "version": COMPANY_REPORT_VERSION,
                "anchor": anchor,
                **{
                    section: _cited_section_dict(
                        getattr(company_model, section),
                        material_count=len(company_selected))
                    for section in COMPANY_REPORT_SECTIONS
                },
                "source_conflicts": _source_conflict_dicts(
                    company_model.source_conflicts,
                    material_count=len(company_selected),
                ),
                "sources": sources_metadata(company_selected),
                "planner": plan.planner,
            }
            company_generated_time = _generated_time_for_today(today)
            company_id = repository.save_research_cache(
                self._db_path, user_id, company, company_report,
                application_id=application_id, generation=generation,
                eligibility_hash=cache_eligibility,
                generated_time=company_generated_time,
                output_locale=output_locale,
                search_profile=company_search_profile)
            if company_id is None:
                return stale()
            company_report_generated_time = company_generated_time
        else:
            company_report = cached_report
            cached = repository.get_research_cache(
                self._db_path, user_id, company, output_locale=output_locale
            )
            if cached is None or cached.get("eligibility_hash") != cache_eligibility:
                return stale()
            company_report_generated_time = cached["research_time"]

        position_material_count = len(position_selected)
        position_report = {
            "key_takeaways": list(position_model.key_takeaways),
            **{
                section: _cited_section_dict(
                    getattr(position_model, section),
                    material_count=position_material_count)
                for section in POSITION_REPORT_SECTIONS
            },
            "reported_questions": [
                {
                    "text": item.text,
                    "category": item.category,
                    "provenance": item.provenance,
                    "sources": [index for index in item.sources
                                if 1 <= index <= position_material_count],
                }
                for item in position_model.reported_questions
            ],
            "likely_questions": [item.model_dump(mode="json") for item in position_model.likely_questions],
            "assessment_focuses": [item.model_dump(mode="json") for item in position_model.assessment_focuses],
            "source_conflicts": _source_conflict_dicts(
                position_model.source_conflicts,
                material_count=position_material_count,
            ),
            "sources": sources_metadata(position_selected),
        }
        position_report_generated_time = _generated_time_for_today(today)
        return {
            "status": "ok",
            "company_report": company_report,
            "company_from_cache": company_model is None,
            "position_report": position_report,
            "anchor": anchor,
            "planner": plan.planner,
            "company_report_generated_time": company_report_generated_time,
            "position_report_generated_time": position_report_generated_time,
            "semantic_claim": semantic_claim,
        }
