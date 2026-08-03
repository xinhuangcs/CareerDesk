"""Read-only chat access to recent status logs and recurring factors."""

from agentmaker import Tool, ToolParameter, ToolResponse

from ...features.personal_state.public import PersonalStateQueries

# English time-of-day code to Chinese label.
_TIME_LABELS = {"morning": "早", "noon": "午", "afternoon": "下午", "evening": "晚", "night": "深夜"}


class QueryStatusTool(Tool):
    """Read recurring factors and recent status records."""

    supports_parallel = True

    def __init__(self, queries: PersonalStateQueries, user_id: str):
        super().__init__(
            "query_status",
            "查询用户复盘时记录的自我状态。action 二选一：patterns=反复出现的状态因素（如『紧张』『没睡好』"
            "各出现几次，重要面试可提醒规避）；recent=最近几条状态记录。用户问「我最近状态有什么规律」时用它。",
            origin="careerdesk",
        )
        self._queries = queries
        self._user_id = user_id

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("action", "string", "查什么", schema={
                "type": "string", "enum": ["patterns", "recent"],
                "description": "patterns=反复出现的状态因素 / recent=最近几条记录"}),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        action = parameters.get("action") or "patterns"
        if action == "patterns":
            factors = self._queries.recurring_factors(self._user_id)
            if not factors:
                return ToolResponse.ok("最近的状态记录里还看不出反复出现的因素（复盘时多记几次状态就能看出规律）。",
                                       data={"factors": []})
            lines = [f"- 「{factor}」最近出现 {count} 次" for factor, count in factors]
            return ToolResponse.ok("你最近反复出现的状态因素（重要面试尽量避开这些状态）：\n" + "\n".join(lines),
                                   data={"factors": factors})
        if action == "recent":
            records = self._queries.recent(self._user_id)
            if not records:
                return ToolResponse.ok("还没有状态记录（复盘时提到累/紧张/没睡好等会自动记下）。",
                                       data={"items": []})
            lines = [f"- {record['log_date']} {_TIME_LABELS.get(record['time_of_day'], record['time_of_day'] or '')}"
                     f"：{record['mood'] or '—'}"
                     + (f"（{'、'.join(record['factors'])}）" if record["factors"] else "")
                     for record in records]
            return ToolResponse.ok("最近的状态记录：\n" + "\n".join(lines), data={"items": records})
        return ToolResponse.error(f"未知 action：{action}（可选 patterns/recent）")
