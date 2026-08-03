"""Code-only company briefing assembly from cached artifacts into Markdown.

Includes the company report, role report, and stored adaptation advice. This module
only formats and normalizes source numbering; it performs no search or model call.
"""

from ...core.config import local_today
from ...features.applications import public as applications
from ...features.research.public import (
    derive_search_profile,
    derive_research_artifact_state,
    get_company_profile,
    get_research_cache,
    research_is_fresh,
    research_semantic_claim_hash,
)
from ...platform.locale import OutputLocale

COMPANY_REPORT_VERSION = 3
COMPANY_SECTION_TITLES = (
    ("business", "业务与产品"),
    ("culture", "文化与口碑"),
    ("recent_news", "近期动态"),
    ("interview_style", "通用面试风格"),
)
POSITION_SECTION_TITLES = (
    ("interview_process", "面试流程与轮次"),
    ("experience_highlights", "岗位面经要点"),
    ("team_and_work_context", "团队与工作语境"),
)
COMPANY_SECTION_TITLES_EN = (
    ("business", "Business and products"),
    ("culture", "Culture and reputation"),
    ("recent_news", "Recent developments"),
    ("interview_style", "General interview style"),
)
POSITION_SECTION_TITLES_EN = (
    ("interview_process", "Interview process and rounds"),
    ("experience_highlights", "Role-specific interview insights"),
    ("team_and_work_context", "Team and work context"),
)


class _SourceLedger:
    """Merge two one-based source indexes into page-wide URL-stable numbering."""

    def __init__(self):
        self.entries: list[dict] = []
        self._number_by_url: dict[str, int] = {}

    def register(self, sources: list[dict] | None) -> dict[object, int]:
        mapping: dict[object, int] = {}
        for source in sources or []:
            url = str(source.get("url") or "")
            index = source.get("source_id", source.get("index"))
            if not url or not isinstance(index, (int, str)) or isinstance(index, bool):
                continue
            number = self._number_by_url.get(url)
            if number is None:
                number = len(self.entries) + 1
                self._number_by_url[url] = number
                self.entries.append({**source, "number": number})
            mapping[index] = number
        return mapping

    @staticmethod
    def cite(indexes: list | None, mapping: dict[object, int], *, locale: OutputLocale) -> str:
        numbers = sorted({mapping[index] for index in indexes or [] if index in mapping})
        if not numbers:
            return ""
        if locale == "en":
            return " (Sources " + ", ".join(str(number) for number in numbers) + ")"
        return "（来源 " + "、".join(str(number) for number in numbers) + "）"


def _section_lines(
    report: dict,
    titles,
    mapping: dict[object, int],
    *,
    locale: OutputLocale,
) -> list[str]:
    lines: list[str] = []
    for field, title in titles:
        section = report.get(field)
        if isinstance(section, dict):
            text = str(section.get("text") or "").strip()
            citation = _SourceLedger.cite(section.get("sources"), mapping, locale=locale)
        else:
            text = str(section or "").strip()
            citation = ""
        if text:
            separator = ": " if locale == "en" else "："
            lines.append(f"- **{title}**{separator}{text}{citation}")
    return lines


def compose_briefing(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    today: str | None = None,
    output_locale: OutputLocale = "zh-CN",
) -> dict:
    """Generate a company research page for an application.

    Returns an ok response with Markdown and structured section data, or an error when
    the application is absent.
    """
    today = today or local_today().isoformat()
    detail = applications.application_detail(db_path, user_id, application_id)
    if detail is None:
        return {"status": "error", "message": f"找不到岗位 #{application_id}"}
    prep = detail.get("prep") if isinstance(detail.get("prep"), dict) else None
    detail = {**detail, "prep": prep}

    profile = get_company_profile(db_path, user_id, detail["company"])
    current_semantic_hash = research_semantic_claim_hash(
        company=detail["company"],
        aliases=profile["aliases"],
        notes=profile["notes"],
        department=detail.get("department"),
        position=detail["position"],
        jd_text=detail.get("jd_text"),
        output_locale=output_locale,
        search_profile=derive_search_profile(
            detail["company"], detail["position"], detail.get("jd_text")
        ),
    )
    artifact = derive_research_artifact_state(
        prep,
        current_semantic_claim_hash=current_semantic_hash,
        content_locale=output_locale,
    )
    snapshot = artifact.get("snapshot")
    if snapshot is not None:
        company_report = snapshot["company_report"]
        position_report = snapshot["position_report"]
        company_sources = snapshot["company_sources"]
        position_sources = snapshot["position_sources"]
        research_stale = artifact["artifact_state"] == "stale"
    else:
        # Support scattered legacy artifacts, but never mix a valid snapshot with cache.
        research_cache = get_research_cache(
            db_path, user_id, detail["company"], output_locale=output_locale
        )
        company_report = research_cache["research"] if research_cache else None
        localized = (prep or {}).get("localized")
        localized_entry = (
            localized.get(output_locale)
            if isinstance(localized, dict) and isinstance(localized.get(output_locale), dict)
            else {}
        )
        position_report = localized_entry.get("position_report")
        if position_report is None and output_locale == "zh-CN":
            position_report = (prep or {}).get("position_report")
        company_sources = company_report.get("sources") \
            if isinstance(company_report, dict) else None
        position_sources = (position_report or {}).get("sources")
        research_stale = bool(company_report) and not research_is_fresh(
            research_cache["research_time"], today,
        )
    company_current = (isinstance(company_report, dict)
                       and company_report.get("version") == COMPANY_REPORT_VERSION)
    attempt_state = ((prep or {}).get("research_attempt") or {}).get("attempt_state") \
        if isinstance((prep or {}).get("research_attempt"), dict) else None
    prep_running = attempt_state in ("pending", "running") or (
        detail.get("prep_status") in ("pending", "running") and attempt_state is None
    )
    localized = (prep or {}).get("localized")
    localized_entry = (
        localized.get(output_locale)
        if isinstance(localized, dict) and isinstance(localized.get(output_locale), dict)
        else {}
    )
    content_entry = localized_entry or ((prep or {}) if output_locale == "zh-CN" else {})
    anchor = localized_entry.get("anchor") or ((prep or {}).get("anchor") if output_locale == "zh-CN" else None) or (
        company_report.get("anchor") if company_current else None
    )

    ledger = _SourceLedger()
    company_mapping = ledger.register(company_sources if company_current else None)
    position_mapping = ledger.register(position_sources)

    lines = [
        f"# Company research: {detail['company']} · {detail['position']}"
        if output_locale == "en"
        else f"# 公司调研：{detail['company']} · {detail['position']}"
    ]

    if anchor and anchor.get("confidence") == "low":
        note = str(anchor.get("note") or "").strip()
        if output_locale == "en":
            lines.append("> ⚠️ Company identity confidence is low"
                         + (f" ({note})" if note else "")
                         + ". If the wrong company was identified, add its official name or website to the company notes and regenerate.")
        else:
            lines.append("> ⚠️ 公司身份置信度低"
                         + (f"（{note}）" if note else "")
                         + "；如锚定错了公司，请在公司备注补充官网或全称后重新生成")
    if research_stale:
        if snapshot is not None:
            lines.append("> ⚠️ The research snapshot is outdated or the role details have changed. Select “Regenerate” below to refresh it." if output_locale == "en" else "> ⚠️ 当前调研快照已过期或岗位输入已变化，点底部「重新生成」可刷新")
        else:
            lines.append("> ⚠️ This company research is over 14 days old. Select “Regenerate” below to refresh it." if output_locale == "en" else "> ⚠️ 公司调研已超过 14 天，点底部「重新生成」可刷新近况")
    if prep_running and not position_report:
        lines.append("> Research and report generation are in progress. This page will update automatically." if output_locale == "en" else "> 正在联网调研并生成报告，完成后本页会自动补全…")

    takeaways = (position_report or {}).get("key_takeaways") or []
    if takeaways:
        lines.append("\n## Essential preparation" if output_locale == "en" else "\n## 考前必读")
        lines += [f"- {item}" for item in takeaways]

    lines.append("\n## Company overview" if output_locale == "en" else "\n## 公司速览")
    if company_report:
        if company_current:
            titles = COMPANY_SECTION_TITLES_EN if output_locale == "en" else COMPANY_SECTION_TITLES
            lines += _section_lines(company_report, titles, company_mapping, locale=output_locale)
        else:
            lines.append("> Legacy research without source citations. Regenerate to create a cited report." if output_locale == "en" else "> 旧版调研（无来源标注）；重新生成可升级为带来源引用的版本")
            titles = COMPANY_SECTION_TITLES_EN if output_locale == "en" else COMPANY_SECTION_TITLES
            lines += _section_lines(company_report, titles, {}, locale=output_locale)
    elif content_entry.get("research") == "unavailable":
        lines.append("- Search providers are temporarily unavailable. Other content is still available; regenerate later to complete the report." if output_locale == "en" else "- 搜索出口暂时不可用；其余内容已正常生成，可稍后重新生成补全")
    elif content_entry.get("research") == "disabled":
        lines.append("- Online company research is disabled. Enable it under Settings → Search API, then regenerate." if output_locale == "en" else "- 联网公司调研未授权；到「设置 → Search API」开启后重新生成")
    else:
        lines.append("- Company research has not been generated yet. This section will update automatically when it is ready." if output_locale == "en" else "- 公司调研还没生成（生成完成后这里会自动补全）")

    if position_report:
        lines.append("\n## What to expect in this interview" if output_locale == "en" else "\n## 这个岗位怎么面")
        titles = POSITION_SECTION_TITLES_EN if output_locale == "en" else POSITION_SECTION_TITLES
        lines += _section_lines(position_report, titles, position_mapping, locale=output_locale)
    likely_questions = (position_report or {}).get("likely_questions") or []
    if not likely_questions and company_report and not company_current:
        # During migration, display v1 predicted questions stored on company reports.
        likely_questions = company_report.get("likely_questions") or []
    if likely_questions:
        lines.append("\n## Likely questions found during research" if output_locale == "en" else "\n## 调研中发现的可能问题")
        lines += [f"- {question.get('text', '')}" for question in likely_questions if isinstance(question, dict)]

    if ledger.entries:
        lines.append("\n## Sources" if output_locale == "en" else "\n## 参考来源")
        for entry in ledger.entries:
            label = entry.get("title") or entry.get("site") or entry.get("url")
            suffix = "、".join(filter(None, [entry.get("site"), entry.get("date")]))
            lines.append(f"{entry['number']}. [{label}]({entry['url']})"
                         + ((f" ({suffix})" if output_locale == "en" else f"（{suffix}）") if suffix else ""))

    footer = []
    prepared_time = content_entry.get("prepared_time")
    if prepared_time:
        footer.append(("Research generated on " if output_locale == "en" else "调研生成于 ") + str(prepared_time)[:10])
    if anchor and anchor.get("official_name"):
        identity = "、".join(filter(None, [
            anchor.get("official_name"),
            anchor.get("website_domain"),
            anchor.get("industry"),
        ]))
        footer.append(("Research identity: " if output_locale == "en" else "本调研基于：") + identity)
    if footer:
        lines.append("\n---\n" + ("; " if output_locale == "en" else "；").join(footer))

    return {"status": "ok", "markdown": "\n".join(lines),
            "data": {"application": detail, "research": company_report,
                     "research_stale": research_stale,
                     "position_report": position_report,
                     "anchor": anchor, "sources": ledger.entries}}
