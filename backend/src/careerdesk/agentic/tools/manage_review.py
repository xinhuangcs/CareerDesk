"""Review corrections via trusted immediate operations or page undo proposals."""

import hashlib
import json
from collections.abc import Callable
from datetime import date
from sqlite3 import Connection
from uuid import UUID, uuid4

from agentmaker import Tool, ToolParameter, ToolResponse

from ...features.reviews import public as reviews
from .request_proposal_write_fence import RequestProposalWriteFence

_OUTCOMES = ["passed", "failed", "cancelled"]
_OUTCOME_LABELS = {"passed": "通过", "failed": "未通过", "cancelled": "取消"}


def _optional_selector(parameters: dict, name: str) -> str | None:
    value = parameters.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    return value.strip() or None


def _canonical_edit_command(parameters: dict) -> tuple[str | None, str | None, dict]:
    """Normalize model-visible input; trusted turn/operation IDs never come from it."""
    company = _optional_selector(parameters, "company")
    position = _optional_selector(parameters, "position")
    changes: dict = {}

    if parameters.get("new_step") is not None:
        step = parameters["new_step"]
        if not isinstance(step, str) or not step.strip():
            raise ValueError("new_step 必须是非空字符串")
        changes["step"] = step.strip()
    if parameters.get("clear_step") is True:
        if "step" in changes:
            raise ValueError("new_step 与 clear_step 不能同时使用")
        changes["step"] = None

    if parameters.get("new_date") is not None:
        occurred_date = parameters["new_date"]
        if not isinstance(occurred_date, str):
            raise ValueError("new_date 必须是有效的 YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(occurred_date)
        except ValueError as error:
            raise ValueError("new_date 必须是有效的 YYYY-MM-DD") from error
        if parsed.isoformat() != occurred_date:
            raise ValueError("new_date 必须是有效的 YYYY-MM-DD")
        changes["occurred_date"] = occurred_date
    if parameters.get("clear_date") is True:
        if "occurred_date" in changes:
            raise ValueError("new_date 与 clear_date 不能同时使用")
        changes["occurred_date"] = None

    if parameters.get("new_outcome") is not None:
        outcome = parameters["new_outcome"]
        if outcome not in _OUTCOMES:
            raise ValueError("new_outcome 不是支持的结果")
        changes["outcome"] = outcome
    if parameters.get("clear_outcome") is True:
        if "outcome" in changes:
            raise ValueError("new_outcome 与 clear_outcome 不能同时使用")
        changes["outcome"] = None

    if parameters.get("new_summary") is not None:
        summary = parameters["new_summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("new_summary 必须是非空字符串")
        changes["summary"] = summary.strip()
    if parameters.get("clear_summary") is True:
        if "summary" in changes:
            raise ValueError("new_summary 与 clear_summary 不能同时使用")
        changes["summary"] = None

    if not changes:
        raise ValueError("没有要改的历程字段")
    return company, position, changes


def _edit_command_hash(company: str | None, position: str | None, changes: dict) -> str:
    encoded = json.dumps(
        {"changes": changes, "company": company, "position": position},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ManageReviewTool(Tool):
    """Undo a whole review or correct one of its structured facts."""

    def __init__(self, db_path: str, user_id: str, *, client_turn_id: str | UUID,
                 request_proposal_write_fence: RequestProposalWriteFence | None = None,
                 proposal_recorder: Callable[[Connection, str, str], object] | None = None):
        super().__init__(
            "manage_review",
            "修正复盘本身记错的内容（不是改投递记录，那用 update_application）。"
            "action=undo：只生成整条撤销的待确认卡，页面批准后才会清掉派生历程、状态日志并归档独有真题；"
            "action=edit_timeline_entry：改这条复盘里的具体环节、发生日期、结果或说明。"
            "默认作用于最近一条复盘；用户指明是哪家的（如「腾讯那条」）就传 company。"
            "每个用户请求最多生成一次撤销卡，不能重试后再定位更早的复盘。撤销/修正复盘会连带调整看板，"
            "与公司官网无关。",
            origin="careerdesk",
        )
        self._db_path = db_path
        self._user_id = user_id
        try:
            self._client_turn_id = str(UUID(str(client_turn_id)))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("client_turn_id 必须是 UUID") from error
        self._operation_ids_by_command: dict[str, str] = {}
        self._terminal_outcomes_by_command: dict[str, ToolResponse] = {}
        self._request_proposal_write_fence = (
            request_proposal_write_fence or RequestProposalWriteFence()
        )
        self._proposal_recorder = proposal_recorder

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("action", "string", "undo（生成整条撤销确认卡）/ edit_timeline_entry（改历程事实）",
                          schema={"type": "string", "enum": ["undo", "edit_timeline_entry"],
                                  "description": "undo=生成整条撤销确认卡 / edit_timeline_entry=改环节/日期/结果/说明"}),
            ToolParameter("company", "string", "指定是哪家公司的那条复盘（不传=最近一条）", required=False),
            ToolParameter("position", "string", "同公司多个岗位时指定哪个岗位的复盘", required=False),
            ToolParameter("new_step", "string", "改成的具体环节（edit_timeline_entry 用）", required=False),
            ToolParameter("clear_step", "boolean", "清除原有具体环节时传 true", required=False),
            ToolParameter("new_date", "string", "改成的发生日期 YYYY-MM-DD（edit_timeline_entry 用）", required=False),
            ToolParameter("clear_date", "boolean", "清除原有发生日期时传 true", required=False),
            ToolParameter("new_outcome", "string", "改成的结果（edit_timeline_entry 用）", required=False,
                          schema={"type": "string", "enum": _OUTCOMES}),
            ToolParameter("clear_outcome", "boolean", "清除原有结果时传 true", required=False),
            ToolParameter("new_summary", "string", "改成的事实说明", required=False),
            ToolParameter("clear_summary", "boolean", "清除原有说明时传 true", required=False),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        blocked = self._request_proposal_write_fence.blocked_response()
        if blocked is not None:
            return blocked
        action = parameters.get("action")
        if action not in {"undo", "edit_timeline_entry"}:
            return ToolResponse.error(f"未知 action：{action}（可选 undo / edit_timeline_entry）")
        try:
            company = _optional_selector(parameters, "company")
            position = _optional_selector(parameters, "position")
        except ValueError as error:
            return ToolResponse.error(str(error))
        selector_label = "·".join(value for value in (company, position) if value) or "当前条件"

        if action == "undo":
        # Freeze before lookup/I/O so failure cannot drift to another review in this turn.
            self._request_proposal_write_fence.trip("review_undo")
            try:
                proposal = reviews.prepare_review_undo_operation(
                    self._db_path,
                    self._user_id,
                    company=company,
                    position=position,
                    proposal_recorder=self._proposal_recorder,
                )
            except (reviews.ReviewOperationConflict, ValueError) as error:
                return ToolResponse.error(str(error) or "无法安全生成复盘撤销预览")
            if proposal.get("status") == "not_found":
                scope = f"{company}的" if company else ""
                return ToolResponse.error(f"没找到{scope}可操作的复盘记录。")
            if proposal.get("status") == "ambiguous":
                options = " / ".join(proposal["options"])
                return ToolResponse.partial(
                    f"{selector_label}匹配到多条复盘（{options}），请向用户确认准确的公司和岗位，"
                    "下一轮同时带 company 与 position 调用。",
                    data=proposal,
                )
            target = proposal["target"]
            preview = target["content_preview"][:40]
            if len(target["content_preview"]) > 40 or target["content_truncated"]:
                preview += "…"
            return ToolResponse.ok(
                f"已生成待确认撤销预览：{target['company']}·{target['position']}「{preview}」。"
                "当前尚未撤销；请让用户核对页面卡片并点击按钮，不能用自然语言代替批准。",
                data=proposal,
            )

        try:
            company, position, changes = _canonical_edit_command(parameters)
        except ValueError as error:
            return ToolResponse.error(str(error))
        command_hash = _edit_command_hash(company, position, changes)
        cached = self._terminal_outcomes_by_command.get(command_hash)
        if cached is not None:
            return cached
        operation_id = self._operation_ids_by_command.setdefault(command_hash, str(uuid4()))
        try:
            result = reviews.execute_review_timeline_entry_edit_operation(
                self._db_path,
                self._user_id,
                operation_id=operation_id,
                client_turn_id=self._client_turn_id,
                company=company,
                position=position,
                changes=changes,
            )
        except reviews.ReviewTimelineEntryEditOperationNotFound:
            return ToolResponse.error(
                "这笔复盘历程修正不存在或不属于当前用户；本次没有写入。",
                data={"reason": "review_timeline_entry_edit_operation_not_found"},
            )
        except reviews.ReviewTimelineEntryEditOperationConflict as error:
            return ToolResponse.error(
                str(error) or "复盘历程修正与已有操作回执冲突；本次没有再次写入。",
                data={"reason": "review_timeline_entry_edit_operation_conflict"},
            )
        except ValueError as error:
            return ToolResponse.error(str(error) or "复盘历程修正参数无效；本次没有写入。")

        if result.get("status") == "not_found":
            scope = f"{company}的" if company else ""
            response = ToolResponse.error(
                f"没找到{scope}可操作的复盘记录。", data=result,
            )
            self._terminal_outcomes_by_command[command_hash] = response
            return response
        if result.get("status") == "ambiguous":
            options = " / ".join(result["options"])
            response = ToolResponse.partial(
                f"{selector_label}匹配到多条复盘（{options}），请向用户确认准确的公司和岗位，"
                "下一轮同时带 company 与 position 调用。",
                data=result,
            )
            self._terminal_outcomes_by_command[command_hash] = response
            return response
        if result.get("status") == "no_change":
            response = ToolResponse.partial(
                "这条复盘历程已经是请求的值，本次无需修改，也没有创建新的可撤销收据。",
                data=result,
            )
            self._terminal_outcomes_by_command[command_hash] = response
            return response
        if result.get("operation_type") != "review_timeline_entry_edit":
            return ToolResponse.error("复盘历程修正返回了无法识别的结果；请查询时间线确认当前状态。")
        if result.get("state") == "undone":
            return ToolResponse.partial(
                "这笔复盘历程修正已经撤销；本次只是幂等重放，没有再次修改复盘。",
                data=result,
            )
        if result.get("state") == "stale":
            return ToolResponse.error(
                "这笔复盘历程修正收据已损坏或失效，本次没有重新执行。",
                data=result,
            )
        if result.get("state") != "completed":
            return ToolResponse.error("复盘历程修正返回了未知状态；请查询时间线确认当前状态。")

        described = "、".join(
            {
                "step": (
                    f"具体环节改为 {result['final']['step']}"
                    if result["final"]["step"] is not None else "具体环节已清除"
                ),
                "occurred_date": (
                    f"日期改为 {result['final']['occurred_date']}"
                    if result["final"]["occurred_date"] is not None else "日期已清除"
                ),
                "outcome": "结果改为 " + (
                    _OUTCOME_LABELS.get(result["final"]["outcome"], result["final"]["outcome"])
                    if result["final"]["outcome"] is not None else "未记录"
                ),
                "summary": "说明已更新" if result["final"]["summary"] else "说明已清除",
            }[field]
            for field in result["effect"]["changed_fields"]
        )
        receipt = (
            "页面已有可信可撤销收据；只有页面撤销按钮能执行 Undo"
            if result.get("undo_available")
            else f"页面保留了操作收据，但当前不可撤销（{result.get('undo_block_reason') or '依赖已变化'}）"
        )
        target = result["target"]
        return ToolResponse.ok(
            f"已修正 {target['company']}·{target['position']} 的复盘历程：{described}。{receipt}。",
            data=result,
        )
