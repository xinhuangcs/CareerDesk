"""Read-only chat access to generated company and role research.

Exposes artifacts otherwise available only on Timeline. It never generates research;
the user's explicit research action remains the only cost-incurring trigger.
"""

from agentmaker import Tool, ToolParameter, ToolResponse

from ...features.applications.public import find_applications_by_company
from ...features.research.public import get_research_cache
from ...orchestration.application_prep.public import (
    compose_briefing,
    inspect_resume_adaptation,
)


class QueryPrepTool(Tool):
    """Read company, role, or resume-adaptation briefing artifacts."""

    supports_parallel = True

    def __init__(self, db_path: str, user_id: str):
        super().__init__(
            "query_prep",
            "Read existing company research, a role briefing, or a resume adaptation report. "
            "All actions require company; position disambiguates multiple roles. This tool never generates artifacts.",
            origin="careerdesk",
        )
        self._db_path = db_path
        self._user_id = user_id

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("action", "string", "Artifact to read", schema={
                "type": "string", "enum": ["company", "briefing", "resume_adaptation"],
                "description": "company report, role briefing, or existing resume adaptation"}),
            ToolParameter("company", "string", "Company name or substring"),
            ToolParameter("position", "string", "Role name when the company has multiple applications", required=False),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        action = parameters["action"]
        raw_company = parameters.get("company")
        if not isinstance(raw_company, str):
            return ToolResponse.error("需要传 company（公司名，简称即可）")
        company = raw_company.strip()
        if not company:
            return ToolResponse.error("需要传 company（公司名，简称即可）")
        raw_position = parameters.get("position")
        if raw_position is not None and not isinstance(raw_position, str):
            return ToolResponse.error("position 必须是岗位名字符串")
        position = None if raw_position is None else (raw_position.strip() or None)
        if action == "company":
            return self._company(company)
        if action == "briefing":
            return self._briefing(company, position)
        if action == "resume_adaptation":
            return self._resume_adaptation(company, position)
        return ToolResponse.error(
            f"未知 action：{action}（可选 company/briefing/resume_adaptation）",
        )

    def _resolve(self, company: str, position: str | None):
        """Resolve by company substring and optional role, returning data or an error."""
        matches = find_applications_by_company(
            self._db_path,
            self._user_id,
            company,
            fuzzy=True,
            position=position,
        )
        if not matches:
            return None, ToolResponse.ok(f"没有公司名含「{company}」"
                                         + (f"、岗位「{position}」" if position else "") + "的投递记录。", data=None)
        if len(matches) > 1:
            options = " / ".join(f"{m['company']}·{m['position']}" for m in matches)
            return None, ToolResponse.partial(
                f"含「{company}」的投递有多条（{options}），请向用户确认要看哪个岗位，带 position 再调一次。",
                data={"matches": matches})
        return matches[0], None

    def _company(self, company: str) -> ToolResponse:
        match, error = self._resolve(company, None)
        # Company research is shared across roles, so the first matching name is enough.
        if error is not None and match is None:
            matches = find_applications_by_company(self._db_path, self._user_id, company, fuzzy=True)
            if not matches:
                return error
            match = matches[0]
        record = get_research_cache(self._db_path, self._user_id, match["company"])
        research = record["research"] if record else None
        if not research:
            return ToolResponse.ok(
                f"还没有{match['company']}的公司调研。可以打开岗位调研页生成。",
                data={
                    "research": None,
                    "ui_actions": [{
                        "kind": "open_application_research",
                        "resource_id": match["id"],
                    }],
                },
            )
        lines = [f"{match['company']} 公司速览："]
        for key, title in (("business", "业务"), ("culture", "文化口碑"),
                           ("recent_news", "近况"), ("interview_style", "面试风格")):
            section = research.get(key)
            text = section.get("text") if isinstance(section, dict) else section
            if text:
                lines.append(f"- {title}：{text}")
        if research.get("likely_questions"):
            # v1 predictions were company-level; v2 stores them in the role report.
            lines.append("- 调研中发现的可能问题：" + "；".join(research["likely_questions"][:5]))
        return ToolResponse.ok(
            "\n".join(lines),
            data={
                "research": research,
                "ui_actions": [{
                    "kind": "open_application_research",
                    "resource_id": match["id"],
                }],
            },
        )

    def _briefing(self, company: str, position: str | None) -> ToolResponse:
        match, error = self._resolve(company, position)
        if error is not None:
            return error
        result = compose_briefing(self._db_path, self._user_id, match["id"])
        if result["status"] != "ok":
            return ToolResponse.ok(result.get("message", "调研页生成失败。"), data=None)
        data = dict(result.get("data") or {})
        data["ui_actions"] = [{
            "kind": "open_application_research",
            "resource_id": match["id"],
        }]
        return ToolResponse.ok(result["markdown"], data=data)

    def _resume_adaptation(self, company: str, position: str | None) -> ToolResponse:
        match, error = self._resolve(company, position)
        if error is not None:
            return error
        state = inspect_resume_adaptation(self._db_path, self._user_id, match["id"])
        action = {"kind": "open_resume_adaptation", "resource_id": match["id"]}
        if state is None:
            return ToolResponse.ok("岗位已不存在，请以当前求职进展为准。", data=None)
        if state.get("state") != "ok" or not isinstance(state.get("report"), dict):
            message = state.get("message") or {
                "no_resume": "该岗位还没有可用简历。",
                "resume_selection_required": "该岗位还没有绑定简历。",
                "missing_jd": "该岗位还没有完整 JD。",
                "stale": "已有简历适配报告已过期，需要在岗位详情中重新生成。",
            }.get(state.get("state"), "该岗位还没有可读取的简历适配报告。")
            return ToolResponse.ok(
                message,
                data={"state": state.get("state"), "ui_actions": [action]},
            )
        report = state["report"]
        summary = list(report.get("summary_sentences") or [])[:3]
        advice = [
            {"action": item.get("action"), "reason": item.get("reason")}
            for item in list(report.get("overall_advice") or [])[:5]
            if isinstance(item, dict)
        ]
        gaps = [
            {"requirement": item.get("requirement_summary"), "basis": item.get("basis")}
            for item in list(report.get("major_gaps") or [])[:3]
            if isinstance(item, dict)
        ]
        next_steps = list(report.get("next_steps") or [])[:3]
        lines = [
            f"简历适配：{match['company']}·{match['position']}（匹配度 {report.get('fit_band')}）",
        ]
        lines.extend(f"- {item}" for item in summary)
        lines.extend(f"- 建议：{item['action']}（{item['reason']}）" for item in advice)
        lines.extend(f"- 关键缺口：{item['requirement']}（{item['basis']}）" for item in gaps)
        lines.extend(f"- 下一步：{item}" for item in next_steps)
        return ToolResponse.ok(
            "\n".join(lines),
            data={
                "application_id": match["id"],
                "fit_band": report.get("fit_band"),
                "summary": summary,
                "advice": advice,
                "major_gaps": gaps,
                "next_steps": next_steps,
                "limitations": list(state.get("host_limitations") or [])[:5],
                "ui_actions": [action],
            },
        )
