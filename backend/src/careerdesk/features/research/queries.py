"""Research query plans, fixed skeletons, normalization, and gap-fill queries.

Skeletons are the zero-LLM fallback when planning fails or produces unusable
output. They keep both company and role legs viable and supply gap-fill terms.
"""

from dataclasses import dataclass, field, replace
import re

# Hard plan bound prevents uncontrolled fan-out from planner output.
MAX_PLANNED_QUERIES = 18
MAX_KEY_QUERIES = 6
MAX_QUERY_CHARS = 120

# Company sections: report field, label, primary suffix, gap suffix, and kind.
COMPANY_SKELETON = [
    ("business", "业务与产品", "主营业务 产品", "公司 介绍 做什么的", "general"),
    ("culture", "文化与口碑", "企业文化 价值观", "员工 评价 工作氛围", "general"),
    ("recent_news", "近期动态", "最新 新闻 动态", "融资 裁员 组织调整", "news"),
    ("interview_style", "通用面试风格", "面试 面经 流程", "校招 面试 几轮 手撕", "general"),
]

# Role sections use the same tuple with ``{position}`` filled from the role name.
POSITION_SKELETON = [
    ("interview_process", "面试流程与轮次", "{position} 面试 流程 几轮", "{position} 校招 面试 安排", "general"),
    ("experience_highlights", "岗位面经要点", "{position} 面经", "{position} 面试 题目 经验", "general"),
    ("team_and_work_context", "团队与工作语境", "{position} 团队 工作方式 专业工具", "团队 业务线 工具 实践", "general"),
    ("reported_questions", "已观察考核形式", "{position} 面试 笔试 案例 作业", "面经 案例 实操 作业", "general"),
    ("likely_questions", "问题预测", "{position} 面试问题 经验", "面试 会问什么", "general"),
    ("assessment_focuses", "考核重点", "{position} 考核重点 能力", "评估标准 考核重点", "general"),
]

COMPANY_SKELETON_EN = [
    ("business", "Business and products", "business products", "company overview what it does", "general"),
    ("culture", "Culture and reputation", "company culture values", "employee reviews work culture", "general"),
    ("recent_news", "Recent developments", "latest news updates", "funding layoffs reorganization", "news"),
    ("interview_style", "Interview style", "interview process experience", "graduate interview rounds coding", "general"),
]

POSITION_SKELETON_EN = [
    ("interview_process", "Interview process", "{position} interview process rounds", "{position} graduate interview schedule", "general"),
    ("experience_highlights", "Interview insights", "{position} interview experience", "{position} interview questions experience", "general"),
    ("team_and_work_context", "Team and work context", "{position} team ways of working tools", "team business unit tools practices", "general"),
    ("reported_questions", "Reported assessments", "{position} interview test case study assignment", "interview case practical assignment", "general"),
    ("likely_questions", "Likely questions", "{position} interview questions experience", "common interview questions", "general"),
    ("assessment_focuses", "Assessment focus", "{position} assessment criteria skills", "evaluation criteria assessment focus", "general"),
]


def derive_search_profile(company: str, position: str, jd_text: str | None = None) -> dict:
    """Derive a bounded search mix from market signals, never from UI/output locale."""
    sample = " ".join(filter(None, (company, position, jd_text or "")))
    primary = "zh" if re.search(r"[\u3400-\u9fff]", sample) else "en"
    return {
        "version": 1,
        "primary_language": primary,
        "secondary_language": "en" if primary == "zh" else "zh",
        "country": None,
    }


@dataclass(frozen=True)
class PlannedQuery:
    """One executable query with leg, section, freshness, and routing hints.

    Key anchor/section queries are cross-checked across providers. ``language``
    is a provider routing hint, not the report output language.
    """

    text: str
    leg: str
    section: str
    kind: str = "general"
    key: bool = False
    language: str | None = None
    country: str | None = None


@dataclass
class QueryPlan:
    """Complete query plan for one research run."""

    queries: list[PlannedQuery] = field(default_factory=list)
    planner: str = "skeleton"  # ``skeleton`` is fixed; ``model`` is AI-planned.

    def company_queries(self) -> list[PlannedQuery]:
        return [query for query in self.queries if query.leg == "company"]

    def position_queries(self) -> list[PlannedQuery]:
        return [query for query in self.queries if query.leg == "position"]


def _clean_query_text(text: str) -> str:
    return " ".join(str(text or "").split())[:MAX_QUERY_CHARS].strip()


def skeleton_plan(company: str, position: str, *, include_company: bool = True,
                  include_position: bool = True, search_profile: dict | None = None) -> QueryPlan:
    """Build a zero-LLM plan with one key query per section."""
    queries: list[PlannedQuery] = []
    profile = search_profile or derive_search_profile(company, position)
    primary = profile["primary_language"]
    company_skeleton = COMPANY_SKELETON if primary == "zh" else COMPANY_SKELETON_EN
    position_skeleton = POSITION_SKELETON if primary == "zh" else POSITION_SKELETON_EN
    if include_company:
        queries += [
            PlannedQuery(text=f"{company} {suffix}", leg="company", section=section,
                         kind=kind, key=True)
            for section, _title, suffix, _fallback, kind in company_skeleton
        ]
    if include_position:
        queries += [
            PlannedQuery(text=f"{company} {suffix.format(position=position)}",
                         leg="position", section=section, kind=kind, key=True)
            for section, _title, suffix, _fallback, kind in position_skeleton
        ]
    queries = [
        replace(query, language=primary, country=profile.get("country"))
        for query in queries
    ]
    return QueryPlan(queries=queries, planner="skeleton")


def gap_fill_queries(company: str, position: str, *, missing_company_sections: list[str],
                     missing_position_sections: list[str],
                     search_profile: dict | None = None) -> list[PlannedQuery]:
    """Build single-provider fallback queries for missing sections."""
    queries: list[PlannedQuery] = []
    profile = search_profile or derive_search_profile(company, position)
    primary = profile["primary_language"]
    company_skeleton = COMPANY_SKELETON if primary == "zh" else COMPANY_SKELETON_EN
    position_skeleton = POSITION_SKELETON if primary == "zh" else POSITION_SKELETON_EN
    queries += [
        PlannedQuery(text=f"{company} {fallback}", leg="company", section=section, kind=kind)
        for section, _title, _suffix, fallback, kind in company_skeleton
        if section in missing_company_sections
    ]
    queries += [
        PlannedQuery(text=f"{company} {fallback.format(position=position)}",
                     leg="position", section=section, kind=kind)
        for section, _title, _suffix, fallback, kind in position_skeleton
        if section in missing_position_sections
    ]
    return [
        replace(query, language=primary, country=profile.get("country"))
        for query in queries
    ]


def normalize_planned_queries(raw_queries: list[PlannedQuery], *, company: str,
                              position: str, search_profile: dict | None = None) -> QueryPlan:
    """Clean, deduplicate, bound, and normalize planner output.

    Excess key queries become ordinary queries. Empty output falls back to the
    fixed skeleton so an executable plan always remains.
    """
    valid_company_sections = {section for section, *_rest in COMPANY_SKELETON}
    valid_position_sections = {section for section, *_rest in POSITION_SKELETON}
    seen: set[str] = set()
    cleaned: list[PlannedQuery] = []
    key_count = 0
    profile = search_profile or derive_search_profile(company, position)
    for query in raw_queries:
        text = _clean_query_text(query.text)
        if not text or text.casefold() in seen:
            continue
        if query.leg == "company":
            section = query.section if query.section in valid_company_sections else "business"
        elif query.leg == "position":
            section = (query.section if query.section in valid_position_sections
                       else "experience_highlights")
        else:
            continue
        kind = query.kind if query.kind in ("general", "news") else "general"
        language = (
            query.language
            if query.language in ("zh", "en")
            else profile["primary_language"]
        )
        key = bool(query.key) and key_count < MAX_KEY_QUERIES
        if key:
            key_count += 1
        seen.add(text.casefold())
        cleaned.append(replace(query, text=text, section=section, kind=kind,
                               key=key, language=language,
                               country=profile.get("country")))
        if len(cleaned) >= MAX_PLANNED_QUERIES:
            break
    if not cleaned:
        return skeleton_plan(company, position, search_profile=search_profile)
    if key_count == 0:
        # If the planner marks no keys, promote each section's first query so the
        # primary coverage is cross-checked.
        promoted: list[PlannedQuery] = []
        promoted_sections: set[tuple[str, str]] = set()
        for query in cleaned:
            marker = (query.leg, query.section)
            if marker not in promoted_sections and len(promoted_sections) < MAX_KEY_QUERIES:
                promoted_sections.add(marker)
                promoted.append(replace(query, key=True))
            else:
                promoted.append(query)
        cleaned = promoted
    return QueryPlan(queries=cleaned, planner="model")
