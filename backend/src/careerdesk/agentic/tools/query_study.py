"""Read-only catalogue and scoped competency queries for the assistant."""

from agentmaker import Tool, ToolParameter, ToolResponse

from ...features.questions import public as questions


class QueryStudyTool(Tool):
    supports_parallel = True

    def __init__(self, db_path: str, user_id: str):
        super().__init__(
            "query_study",
            "查询题库与练习沉淀。action: overview、questions、weak_points、knowledge。"
            "能力进度会同时返回全局汇总与基础版/定制版/题库快照的独立范围。",
            origin="careerdesk",
        )
        self._db_path = db_path
        self._user_id = user_id

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("action", "string", "查询类型", schema={
                "type": "string", "enum": ["overview", "questions", "weak_points", "knowledge"]}),
            ToolParameter("source", "string", "题目来源", required=False),
            ToolParameter("company", "string", "公司（仅真实题出处）", required=False),
            ToolParameter("knowledge_point", "string", "能力名称", required=False),
            ToolParameter("category", "string", "题目类别", required=False),
            ToolParameter("channel", "string", "interview 或 written", required=False),
            ToolParameter("name", "string", "能力名称模糊匹配", required=False),
            ToolParameter("limit", "integer", "最多返回条数", required=False, default=10),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        action_link = [{"kind": "open_questions"}]
        action = parameters["action"]
        if action == "overview":
            data = questions.question_overview(
                self._db_path, self._user_id, company=parameters.get("company"),
                knowledge_point=parameters.get("knowledge_point"), source=parameters.get("source"),
            )
            return ToolResponse.ok(
                f"题库共 {data['total']} 道，练过 {data['practiced']} 道。",
                data={**data, "ui_actions": action_link},
            )
        if action == "questions":
            items = questions.list_questions(
                self._db_path, self._user_id, source=parameters.get("source"),
                company=parameters.get("company"), knowledge_point=parameters.get("knowledge_point"),
                category=parameters.get("category"), channel=parameters.get("channel"),
                limit=max(1, min(int(parameters.get("limit") or 10), 100)),
            )
            lines = [f"- [{item['category']}/{item['channel']}] {item['text']}"
                     f"（能力：{item['primary_competency']}）" for item in items]
            return ToolResponse.ok(
                "\n".join(lines) if lines else "没有符合条件的题目。",
                data={"items": items, "ui_actions": action_link},
            )
        if action == "weak_points":
            items = questions.list_weak_points(
                self._db_path, self._user_id,
                limit=max(1, min(int(parameters.get("limit") or 10), 100)),
            )
            lines = [f"- {item['name']}：{item['box']} 号盒，练习 {item['question_count']} 次" for item in items]
            return ToolResponse.ok(
                "\n".join(lines) if lines else "还没有练习沉淀。",
                data={"items": items, "ui_actions": action_link},
            )
        if action == "knowledge":
            name = parameters.get("name")
            data = (questions.find_knowledge_points(self._db_path, self._user_id, name)
                    if name else questions.competency_overview(self._db_path, self._user_id))
            response_data = ({**data, "ui_actions": action_link}
                             if isinstance(data, dict) else data)
            return ToolResponse.ok(
                "已返回按范围隔离的能力进度。",
                data=response_data,
            )
        return ToolResponse.error("未知 action")
