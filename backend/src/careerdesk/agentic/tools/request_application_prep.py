"""Explicit, bounded Agent command for starting an existing application's prep job."""

import asyncio
import logging

from agentmaker import Tool, ToolParameter, ToolResponse

from ...core.config import get_settings
from ...features.applications.public import find_applications_by_company
from ...orchestration.application_prep.public import (
    PrepApplicationNotFound,
    request_prep_generation,
)
from ...platform.ai.client import OutboundAccessDisabled
from ...platform.locale import OutputLocale

logger = logging.getLogger(__name__)
_BACKGROUND_PREP_TASKS: set[asyncio.Task] = set()


def _schedule_background_prep(service, user_id: str, application_id: int, **kwargs) -> None:
    """Keep a strong reference until the durable job reaches a terminal state."""
    task = asyncio.create_task(
        service.run(user_id, application_id, **kwargs),
        name=f"careerdesk-agent-prep-{application_id}",
    )
    _BACKGROUND_PREP_TASKS.add(task)

    def completed(done: asyncio.Task) -> None:
        _BACKGROUND_PREP_TASKS.discard(done)
        if done.cancelled():
            return
        try:
            done.result()
        except Exception:  # pragma: no cover - PrepService normally terminalizes errors
            logger.exception("agent-started prep task escaped its service boundary")

    task.add_done_callback(completed)


class RequestApplicationPrepTool(Tool):
    """Start, retry or explicitly refresh the existing Application Prep workflow."""

    supports_parallel = False

    def __init__(self, user_id: str, *, output_locale: OutputLocale = "zh-CN"):
        super().__init__(
            "request_application_prep",
            "为一条已经存在的精确岗位启动公司与岗位调研。start=首次生成；retry=失败或过期任务重试；"
            "refresh=用户明确要求重新生成/刷新时使用，可能按当前设置联网。"
            "不要用它查询现有报告；查询用 query_prep。每轮最多尝试一次。",
            origin="careerdesk",
        )
        self._user_id = user_id
        self._output_locale = output_locale
        self._attempted = False

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("company", "string", "现有投递记录里的公司名（简称可以）"),
            ToolParameter("position", "string", "同公司多岗时用于精确定位", required=False),
            ToolParameter(
                "action",
                "string",
                "start=首次生成 / retry=失败或过期重试 / refresh=明确重新生成",
                schema={"type": "string", "enum": ["start", "retry", "refresh"]},
            ),
        ]

    async def arun(self, parameters: dict) -> ToolResponse:
        if self._attempted:
            return ToolResponse.error("本轮已经尝试过一次调研请求；请等待当前结果，不要重复启动。")
        self._attempted = True
        company = parameters.get("company")
        position = parameters.get("position")
        action = parameters.get("action")
        if not isinstance(company, str) or not company.strip():
            return ToolResponse.error("需要提供现有岗位的公司名")
        if position is not None and (not isinstance(position, str) or not position.strip()):
            return ToolResponse.error("position 必须是非空岗位名")
        if action not in {"start", "retry", "refresh"}:
            return ToolResponse.error("action 只能是 start、retry 或 refresh")
        settings = get_settings()
        matches = find_applications_by_company(
            settings.db_path,
            self._user_id,
            company.strip(),
            fuzzy=True,
            position=position.strip() if isinstance(position, str) else None,
        )
        if not matches:
            return ToolResponse.error("没有找到匹配的现有岗位；请先把岗位加入求职进展。")
        if len(matches) > 1:
            options = " / ".join(f"{item['company']}·{item['position']}" for item in matches)
            return ToolResponse.partial(
                f"匹配到多个岗位（{options}），请向用户确认准确岗位后再启动调研。",
                data={"matches": matches},
            )
        target = matches[0]
        try:
            result = await request_prep_generation(
                settings,
                self._user_id,
                target["id"],
                force=action == "retry",
                refresh_research=action == "refresh",
                schedule=_schedule_background_prep,
                output_locale=self._output_locale,
            )
        except PrepApplicationNotFound:
            return ToolResponse.error("岗位刚被删除或合并，本次没有启动调研。")
        except OutboundAccessDisabled as error:
            return ToolResponse.error(str(error))
        ui_action = {"kind": "open_application_research", "resource_id": target["id"]}
        data = {**result, "ui_actions": [ui_action]}
        if result["status"] == "error":
            return ToolResponse.error(result.get("message") or "调研任务没有启动", data=data)
        if result["status"] in {"reused", "completed"}:
            return ToolResponse.ok(
                result.get("message") or "已有调研任务或可用结果，没有重复调用模型。",
                data=data,
            )
        refresh_note = (
            "；已按当前设置请求刷新联网材料"
            if result.get("refresh_applied")
            else "；本次没有联网刷新公司材料"
            if action == "refresh"
            else ""
        )
        return ToolResponse.ok(
            f"已开始生成 {target['company']}·{target['position']} 的公司与岗位调研{refresh_note}。"
            "任务会在后台继续，可打开岗位调研查看进度。",
            data=data,
        )


__all__ = ["RequestApplicationPrepTool"]
