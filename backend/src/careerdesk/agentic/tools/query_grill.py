"""Read-only session history, detailed feedback and neutral practice statistics."""

import json

from agentmaker import Tool, ToolParameter, ToolResponse

from ...features.grill import public as grill

_VERDICTS = {"meets": "表现良好", "partially_meets": "尚可", "needs_work": "需改进",
             "ungradable": "暂无法评价", "skipped": "跳过"}
_STATES = {"active": "进行中", "suspended": "挂起", "finished": "已结束"}
_MAX_DETAIL_ANSWERS = 20
_MAX_FEEDBACK_CHARS = 2_000
_MAX_QUESTION_CHARS = 1_000
_DEFAULT_SESSION_LIMIT = 20
_MAX_SESSION_LIMIT = 50


def _string_list(value: object, *, limit: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)][:limit]


def _excerpt(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= limit else f"{value[:limit]}…"


class QueryGrillTool(Tool):
    supports_parallel = True

    def __init__(self, db_path: str, user_id: str):
        super().__init__("query_grill", "查询面试模拟练习的统计、历史、挂起场次或某场已结束练习的详细反馈。"
                         "stats/history/suspended 可按冻结上下文标签过滤；session_detail 需要 session_id。",
                         origin="careerdesk")
        self._db_path, self._user_id = db_path, user_id

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("action", "string", "查询类型", schema={"type": "string",
                          "enum": ["stats", "history", "suspended", "session_detail"]}),
            ToolParameter("context", "string", "按简历或岗位上下文标签过滤", required=False),
            ToolParameter("session_id", "integer", "已结束练习的精确场次 ID（仅 session_detail）",
                          required=False, schema={"type": "integer", "minimum": 1}),
            ToolParameter("limit", "integer", "历史/挂起场次最多返回条数（默认20，最大50）",
                          required=False,
                          schema={"type": "integer", "minimum": 1, "maximum": 50}),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        action = parameters["action"]
        if action == "session_detail":
            return self._session_detail(parameters.get("session_id"))
        if action == "stats":
            data = grill.grill_overview(self._db_path, self._user_id,
                                        context=parameters.get("context"))
            detail = "、".join(f"{_VERDICTS.get(key, key)} {value}"
                                for key, value in data["verdicts"].items())
            return ToolResponse.ok(
                f"共 {data['total_sessions']} 场练习、{data['total_answers']} 道作答。"
                + (f"\n评价分布：{detail}。" if detail else ""),
                data={**data, "ui_actions": [{"kind": "open_grill"}]},
            )
        raw_limit = parameters.get("limit", _DEFAULT_SESSION_LIMIT)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) \
                or not 1 <= raw_limit <= _MAX_SESSION_LIMIT:
            return ToolResponse.error("limit 必须是 1–50 的整数")
        states = ["finished"] if action == "history" else ["active", "suspended"]
        items = grill.list_sessions(self._db_path, self._user_id, states)
        context = parameters.get("context")
        if context is not None:
            if not isinstance(context, str) or not context.strip():
                return ToolResponse.error("context 必须是非空字符串")
            expected = context.strip().casefold()
            items = [item for item in items if expected in item["context_label"].casefold()]
        total = len(items)
        items = items[:raw_limit]
        lines = [f"- {item['context_label']}（{_STATES[item['state']]}，"
                 f"{item['answered']}/{item['total']}）" for item in items]
        ui_actions = ([{
            "kind": "open_grill_session", "resource_id": items[0]["id"],
        }] if len(items) == 1 else [{"kind": "open_grill"}]) if items else []
        return ToolResponse.ok(("\n".join(lines) if lines else "没有匹配的练习场次。"),
                               data={"items": items, "truncated": total > len(items),
                                     "ui_actions": ui_actions})

    def _session_detail(self, raw_session_id) -> ToolResponse:
        if (isinstance(raw_session_id, bool) or not isinstance(raw_session_id, int)
                or raw_session_id <= 0):
            return ToolResponse.error(
                "session_detail 需要正整数 session_id；请先用 history 查询场次。",
            )
        session = grill.replay(self._db_path, self._user_id, raw_session_id)
        if session is None:
            return ToolResponse.ok(
                "找不到这场已结束练习；进行中或挂起场次只能在拷打室继续。",
                data={"session": None},
            )
        answers = []
        lines = []
        for item in session.get("answers", [])[:_MAX_DETAIL_ANSWERS]:
            raw_feedback = item.get("feedback")
            feedback = raw_feedback if isinstance(raw_feedback, dict) else {}
            compact_feedback = {
                "strengths": _string_list(feedback.get("strengths")),
                "gaps": _string_list(feedback.get("gaps")),
                "next_step": feedback.get("next_step")
                if isinstance(feedback.get("next_step"), str) else None,
            }
            rendered = json.dumps(compact_feedback, ensure_ascii=False, separators=(",", ":"))
            if len(rendered) > _MAX_FEEDBACK_CHARS:
                compact_feedback = {
                    "strengths": [],
                    "gaps": [rendered[:_MAX_FEEDBACK_CHARS - 200] + "…"],
                    "next_step": None,
                }
            question = _excerpt(item.get("text"), _MAX_QUESTION_CHARS)
            answers.append({
                "session_item_id": item.get("session_item_id"),
                "question": question,
                "competency": item.get("primary_competency"),
                "verdict": item.get("verdict"),
                "stuck": bool(item.get("stuck")),
                "feedback": compact_feedback,
            })
            gaps = "；".join(compact_feedback["gaps"]) or "无明确缺口"
            next_step = compact_feedback.get("next_step")
            lines.append(
                f"- {question or '未命名题目'}："
                f"{_VERDICTS.get(item.get('verdict'), item.get('verdict') or '未知')}；"
                f"缺口：{gaps}" + (f"；下一步：{next_step}" if next_step else "")
            )
        return ToolResponse.ok(
            f"{session['context_label']}的练习反馈（{len(answers)} 题）：\n"
            + ("\n".join(lines) if lines else "暂无可展示的反馈"),
            data={
                "session": {
                    "id": session["session_id"],
                    "context_label": session["context_label"],
                    "summary": session.get("summary") or {},
                    "answers": answers,
                },
                "ui_actions": [{
                    "kind": "open_grill_session",
                    "resource_id": session["session_id"],
                }],
            },
        )
