"""Batch application proposal tool that parses previews without persistence authority.

Previews are journaled under server-issued operation IDs. Only the page confirmation
endpoint can persist them to fixed board tables.
"""

from agentmaker import Tool, ToolParameter, ToolResponse

from ...features.applications.public import (
    ApplicationService,
    STAGE_LABELS,
    standard_positions_from_structured_text,
)
from .request_proposal_write_fence import RequestProposalWriteFence


def _preview_lines(positions: list[dict]) -> str:
    """Render numbered preview rows aligned with page exclusion controls."""
    lines = []
    for index, item in enumerate(positions, start=1):
        stage = STAGE_LABELS.get(item.get("stage"), item.get("stage"))
        next_action = item.get("next_action") or {}
        extras = "，".join(filter(None, [
            stage and f"阶段：{stage}",
            item.get("department"),
            item.get("channel"),
            next_action.get("step") and (
                f"下一步 {next_action['step']}"
                + (f"（{next_action['date']}）" if next_action.get("date") else "")
            ),
        ]))
        flag = " ⚠️已投过（确认后更新旧记录）" if item.get("already_exists") else ""
        lines.append(f"{index}. {item['company']}·{item['position']}"
                     + (f"（{extras}）" if extras else "") + flag)
    return "\n".join(lines)


class ParseJobsTool(Tool):
    """Parse requested role/JD imports into a numbered preview without writes."""

    def __init__(self, service: ApplicationService, user_id: str, *,
                 request_proposal_write_fence: RequestProposalWriteFence | None = None):
        super().__init__(
            "parse_jobs",
            "用户明确要求导入/入库新岗位（即使一条），或一次粘贴多个岗位信息时调用；"
            "输入可以是 JD 全文、招聘页文字、公司-岗位列表，或本地代码从 Excel/CSV"
            "提取的完整结构化原文；每批最多 200 条。"
            "解析成结构化条目并返回预览清单。本工具只创建 proposal，不落库；"
            "引导用户在页面确认卡中剔除条目并确认或取消。",
            origin="careerdesk",
        )
        self._service = service
        self._user_id = user_id
        self._request_proposal_write_fence = (
            request_proposal_write_fence or RequestProposalWriteFence()
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [ToolParameter(
            "text", "string",
            "用户粘贴的岗位原文；表格附件必须连同 CAREERDESK_STANDARD_ROWS 块完整原样传入",
        )]

    async def arun(self, parameters: dict) -> ToolResponse:
        """Execute asynchronously because parsing awaits the model."""
        blocked = self._request_proposal_write_fence.blocked_response()
        if blocked is not None:
            return blocked
        self._request_proposal_write_fence.trip("job_intake")
        text = parameters["text"]
        standard_payload = standard_positions_from_structured_text(text)
        if standard_payload is not None:
            result = self._service.parse_standard_positions(
                self._user_id,
                list(standard_payload.positions),
                source_label="Agent 标准表格附件",
                source_rows=standard_payload.source_rows,
                skipped_rows=standard_payload.skipped_rows,
            )
        else:
            result = await self._service.parse_batch(self._user_id, text)
        if result["status"] == "superseded":
            return ToolResponse.partial("这次解析已被更新的岗位批次取代，请以最新预览为准。", data=result)
        if result["status"] == "empty":
            return ToolResponse.partial("没有解析出任何岗位（公司名和岗位名都拆不出来的内容会被丢弃），"
                                        "请让用户补充公司与岗位信息。", data=result)
        preview = _preview_lines(result["positions"])
        return ToolResponse.ok(
            f"解析出 {len(result['positions'])} 条岗位：\n{preview}\n"
            "预览已交给页面确认卡。请提醒用户在卡片中确认或取消；"
            "自然语言的「确认」不是落盘授权。",
            data=result,
        )
