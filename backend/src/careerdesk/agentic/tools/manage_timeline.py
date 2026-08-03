"""Timeline correction tools for undoable updates and high-risk deletion proposals.

Updates execute atomically with trusted operation/turn IDs and produce undo receipts.
Deletion only freezes a server operation for page approval. Ambiguous company matches
return candidates instead of guessing a role.
"""

from collections.abc import Callable
from datetime import date
from sqlite3 import Connection
from uuid import UUID, uuid4

from agentmaker import Tool, ToolParameter, ToolResponse

from ...features.applications import public as applications
from .request_proposal_write_fence import RequestProposalWriteFence

# Stage code to Chinese receipt label; schema enum and table CHECK validate input.
_STAGE_CHOICES = list(applications.STAGE_LABELS)
_UPDATE_ITEM_FIELDS = frozenset({
    "company", "position", "new_position", "new_company", "new_stage",
    "new_current_step", "clear_current_step", "new_priority", "clear_priority",
    "next_stage", "next_step", "next_date", "next_time", "next_note",
    "clear_next_date", "clear_next_time", "clear_next_note", "clear_next_action",
    "new_jd_text", "new_note", "append_note", "replacement_note", "clear_note",
})
_UPDATE_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["company"],
    "properties": {
        "company": {"type": "string", "minLength": 1},
        "position": {"type": "string", "minLength": 1},
        "new_position": {"type": "string", "minLength": 1},
        "new_company": {"type": "string", "minLength": 1},
        "new_stage": {"type": "string", "enum": _STAGE_CHOICES},
        "new_current_step": {"type": "string", "minLength": 1},
        "clear_current_step": {"type": "boolean", "const": True},
        "new_priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "clear_priority": {"type": "boolean", "const": True},
        "next_stage": {"type": "string", "enum": _STAGE_CHOICES},
        "next_step": {"type": "string", "minLength": 1},
        "next_date": {"type": "string", "format": "date"},
        "next_time": {"type": "string", "minLength": 1},
        "next_note": {"type": "string", "minLength": 1},
        "clear_next_date": {"type": "boolean", "const": True},
        "clear_next_time": {"type": "boolean", "const": True},
        "clear_next_note": {"type": "boolean", "const": True},
        "clear_next_action": {"type": "boolean", "const": True},
        "new_jd_text": {"type": "string", "minLength": 1},
        "new_note": {"type": "string", "minLength": 1, "maxLength": 2_000},
        "append_note": {"type": "string", "minLength": 1, "maxLength": 2_000},
        "replacement_note": {"type": "string", "minLength": 1, "maxLength": 2_000},
        "clear_note": {"type": "boolean", "const": True},
    },
}


def _canonical_update_command(
    parameters: dict,
) -> tuple[
    str,
    str | None,
    dict[str, object],
    dict[str, object] | None,
    tuple[str, str | None] | None,
]:
    """Normalize selectors/changes; trusted IDs never come from model parameters."""
    company_value = parameters.get("company")
    if not isinstance(company_value, str) or not company_value.strip():
        raise ValueError("company 不能为空")
    company = company_value.strip()

    position_value = parameters.get("position")
    if position_value is not None and not isinstance(position_value, str):
        raise ValueError("position 必须是字符串")
    position = None if position_value is None else position_value.strip() or None

    changes: dict[str, object] = {}
    for key in ("company", "position", "stage"):
        parameter_name = f"new_{key}"
        if parameter_name not in parameters:
            continue
        value = parameters[parameter_name]
        if not isinstance(value, str):
            raise ValueError(f"{parameter_name} 必须是字符串")
        canonical = value.strip()
        if not canonical:
            raise ValueError(f"{parameter_name} 不能为空")
        changes[key] = canonical
    if "new_jd_text" in parameters:
        value = parameters["new_jd_text"]
        if not isinstance(value, str):
            raise ValueError("new_jd_text 必须是字符串")
        if not value.strip():
            raise ValueError("new_jd_text 不能为空")
        changes["jd_text"] = value.strip()
    if "new_current_step" in parameters and "clear_current_step" in parameters:
        raise ValueError("设置与清空当前环节不能同时执行")
    if "new_current_step" in parameters:
        value = parameters["new_current_step"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("new_current_step 必须是非空字符串")
        changes["current_step"] = value.strip()
    if "clear_current_step" in parameters:
        if parameters["clear_current_step"] is not True:
            raise ValueError("clear_current_step 只能传 true")
        changes["current_step"] = None
    if "new_priority" in parameters and "clear_priority" in parameters:
        raise ValueError("设置与清除优先级不能同时执行")
    if "new_priority" in parameters:
        priority = parameters["new_priority"]
        if priority not in {"high", "medium", "low"}:
            raise ValueError("new_priority 必须是 high、medium 或 low")
        changes["priority"] = priority
    if "clear_priority" in parameters:
        if parameters["clear_priority"] is not True:
            raise ValueError("clear_priority 只能传 true")
        changes["priority"] = None

    next_fields = ("next_stage", "next_step", "next_date", "next_time", "next_note")
    clear_next_fields = ("clear_next_date", "clear_next_time", "clear_next_note")
    has_next_patch = any(name in parameters for name in (*next_fields, *clear_next_fields))
    clear_next_action = parameters.get("clear_next_action")
    if "clear_next_action" in parameters and clear_next_action is not True:
        raise ValueError("clear_next_action 只能传 true")
    if clear_next_action is True and has_next_patch:
        raise ValueError("清空下一步不能与设置下一步字段同时执行")
    next_action_patch: dict[str, object] | None = None
    if clear_next_action is True:
        changes["next_action"] = None
    elif has_next_patch:
        next_action_patch = {}
        for value_field, clear_field in (
            ("next_date", "clear_next_date"),
            ("next_time", "clear_next_time"),
            ("next_note", "clear_next_note"),
        ):
            if value_field in parameters and clear_field in parameters:
                raise ValueError(f"{value_field} 与 {clear_field} 不能同时设置")
        for field in next_fields:
            if field not in parameters:
                continue
            value = parameters[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} 必须是非空字符串")
            next_action_patch[field.removeprefix("next_")] = value.strip()
        for field in clear_next_fields:
            if field not in parameters:
                continue
            if parameters[field] is not True:
                raise ValueError(f"{field} 只能传 true")
            next_action_patch[field.removeprefix("clear_next_")] = None
        if next_action_patch.get("date", ...) is None:
            if next_action_patch.get("time", ...) not in (..., None):
                raise ValueError("清空下一步日期时不能同时设置下一步时间")
            # Time has no meaning without a date; clearing date atomically clears both.
            next_action_patch["time"] = None
        next_date = next_action_patch.get("date", ...)
        if next_date is not ... and next_date is not None:
            try:
                parsed = date.fromisoformat(next_date)
            except (TypeError, ValueError) as error:
                raise ValueError("next_date 必须是真实的 YYYY-MM-DD") from error
            if parsed.isoformat() != next_date:
                raise ValueError("next_date 必须是真实的 YYYY-MM-DD")
    note_parameters = [
        name for name in ("new_note", "append_note", "replacement_note", "clear_note")
        if name in parameters
    ]
    if len(note_parameters) > 1:
        raise ValueError("追加、替换与清空备注不能同时执行")
    note_intent: tuple[str, str | None] | None = None
    if note_parameters and note_parameters[0] != "clear_note":
        parameter_name = note_parameters[0]
        value = parameters[parameter_name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{parameter_name} 不能为空")
        if len(value.strip()) > 2_000:
            raise ValueError(f"{parameter_name} 不能超过 2000 个字符")
        note_intent = (
            "replace" if parameter_name == "replacement_note" else "append",
            value.strip(),
        )
    if note_parameters == ["clear_note"]:
        if parameters["clear_note"] is not True:
            raise ValueError("clear_note 只能传 true")
        note_intent = ("clear", None)
    if note_intent is not None and (changes or next_action_patch is not None):
        raise ValueError("岗位备注请单独修改，不要与身份、阶段、下一步或 JD 混在同一次写入。")
    if not changes and next_action_patch is None and note_intent is None:
        raise ValueError(
            "没有要改的内容：岗位字段、阶段、当前环节、下一步、JD 或备注至少传一项。",
        )
    return company, position, changes, next_action_patch, note_intent


class UpdateApplicationTool(Tool):
    """Atomically update one or more application records from one bounded request."""

    def __init__(self, db_path: str, user_id: str, *,
                 client_turn_id: str | UUID,
                 local_date: str | None = None,
                 request_proposal_write_fence: RequestProposalWriteFence | None = None,
                 proposal_recorder: Callable[[Connection, str, str], object] | None = None):
        super().__init__(
            "update_application",
            "只用于用户明确要求纠正已有看板字段，例如把阶段改回某值、改岗位名、优先级、JD 或备注。"
            "不得处理用户叙述的投递、测评、面试、结果、邀请或日程等真实发生或新确认的进展；这些一律调用一次"
            "无参数 record_review，即使它们会影响阶段、当前环节或下一步。一次原子修改一条或多条已有岗位记录。"
            "始终把完整目标放进唯一的 updates 数组；即使只有一条也必须用单元素数组。整批任一项未通过校验，"
            "所有项都不写入。一个用户请求最多调用本工具一次，最多 20 条。用户说「不跟了/主动放弃/撤回申请」"
            "时传 new_stage=withdrawn；rejected 只表示被公司拒绝。company/position 填现在记录里的名字，"
            "new_* 只填要改的项。多条批量不能修改 company/position；身份修改必须单独作为一条请求。"
            "普通修改成功后页面会显示每条可信可撤销收据；自然语言不能执行 Undo。"
            "任一高风险确认卡尝试后，本轮禁用五个求职记录/proposal 工具；查询与偏好仍可用。",
            origin="careerdesk",
        )
        self._db_path = db_path
        self._user_id = user_id
        try:
            self._client_turn_id = str(UUID(str(client_turn_id)))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("client_turn_id 必须是 UUID") from error
        resolved_date = local_date or date.today().isoformat()
        try:
            parsed_date = date.fromisoformat(resolved_date)
        except (TypeError, ValueError) as error:
            raise ValueError("local_date 必须是真实的 YYYY-MM-DD") from error
        if parsed_date.isoformat() != resolved_date:
            raise ValueError("local_date 必须是真实的 YYYY-MM-DD")
        self._local_date = resolved_date
        self._attempted = False
        self._request_proposal_write_fence = (
            request_proposal_write_fence or RequestProposalWriteFence()
        )
        self._proposal_recorder = proposal_recorder

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                "updates",
                "array",
                "本次请求的完整岗位修改数组（1–20 条）；单条也必须放进数组。每项 company 必填，"
                "position 在同公司有多条记录时必填；只传用户明确要求修改的 new_*/clear_*/next_* 字段。",
                schema={
                    "type": "array",
                    "minItems": 1,
                    "maxItems": applications.MAX_APPLICATION_UPDATE_BATCH_ITEMS,
                    "items": _UPDATE_ITEM_SCHEMA,
                },
            ),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        """Resolve and execute exactly one bounded all-or-nothing update batch."""
        blocked = self._request_proposal_write_fence.blocked_response()
        if blocked is not None:
            return blocked
        if self._attempted:
            return ToolResponse.error(
                "本轮已经尝试过一次岗位修改；为避免重复写入，本轮不能再次调用。"
            )
        self._attempted = True
        if not isinstance(parameters, dict) or set(parameters) != {"updates"}:
            return ToolResponse.error("岗位修改只接受唯一的 updates 数组参数")
        requested_updates = parameters["updates"]
        if not isinstance(requested_updates, list) or not (
            1 <= len(requested_updates) <= applications.MAX_APPLICATION_UPDATE_BATCH_ITEMS
        ):
            return ToolResponse.error(
                f"updates 必须包含 1–{applications.MAX_APPLICATION_UPDATE_BATCH_ITEMS} 条修改"
            )

        prepared: list[dict] = []
        operation_ids: list[str] = []
        selector_keys: set[tuple[str, str]] = set()
        for index, item in enumerate(requested_updates):
            if not isinstance(item, dict):
                return ToolResponse.error(f"updates[{index}] 必须是对象")
            unknown = set(item) - _UPDATE_ITEM_FIELDS
            if unknown:
                return ToolResponse.error(f"updates[{index}] 含不受支持的字段")
            try:
                company, position, changes, next_action_patch, note_intent = (
                    _canonical_update_command(item)
                )
            except ValueError as error:
                return ToolResponse.error(f"updates[{index}]：{error}")
            selector_key = (company.casefold(), (position or "").casefold())
            if selector_key in selector_keys:
                return ToolResponse.error(f"updates[{index}] 与前面的岗位目标重复")
            selector_keys.add(selector_key)
            if len(requested_updates) > 1 and {"company", "position"} & set(changes):
                return ToolResponse.error(
                    "批量修改不能更改公司名或岗位名；身份修改请作为单条请求单独执行"
                )

            expected_application_id: int | None = None
            expected_revision: int | None = None
            if next_action_patch is not None or note_intent is not None:
                resolved = applications.resolve_application_by_name(
                    self._db_path, self._user_id, company, position,
                )
                if resolved["status"] == "not_found":
                    return ToolResponse.error(
                        f"updates[{index}] 没找到精确匹配的投递记录：{company}；整批没有写入。"
                    )
                if resolved["status"] == "ambiguous":
                    return ToolResponse.partial(
                        f"updates[{index}] 的 {company} 有多条投递记录（"
                        f"{' / '.join(resolved['options'])}）；整批没有写入，请先确认岗位。",
                        data={
                            "operation_type": "application_update_batch",
                            "state": "rejected",
                            "requested_count": len(requested_updates),
                            "issues": [{
                                "index": index,
                                "reason": "ambiguous",
                                "options": resolved["options"],
                            }],
                        },
                    )
                detail = applications.application_detail(
                    self._db_path, self._user_id, resolved["id"],
                )
                if detail is None:
                    return ToolResponse.error(
                        f"updates[{index}] 的岗位刚被删除或合并；整批没有写入。"
                    )
                expected_application_id = detail["id"]
                expected_revision = detail["revision"]
                if next_action_patch is not None:
                    merged = dict(detail.get("next_action") or {})
                    merged.update(next_action_patch)
                    if not merged.get("stage") or not merged.get("step"):
                        return ToolResponse.error(
                            f"updates[{index}] 新增下一步必须同时提供 next_stage 与 next_step；"
                            "整批没有写入。"
                        )
                    changes["next_action"] = {
                        "stage": merged["stage"],
                        "step": merged["step"],
                        "date": merged.get("date"),
                        "time": merged.get("time"),
                        "note": merged.get("note"),
                    }
                if note_intent is not None:
                    action, value = note_intent
                    if action == "clear":
                        changes["application_note"] = None
                    elif action == "replace":
                        changes["application_note"] = value
                    else:
                        current_note = detail.get("application_note")
                        combined = f"{current_note}\n{value}" if current_note else value
                        if len(combined) > 2_000:
                            return ToolResponse.error(
                                f"updates[{index}] 的岗位备注不能超过 2000 个字符；整批没有写入。"
                            )
                        changes["application_note"] = combined
            prepared.append({
                "company": company,
                "position": position,
                "changes": changes,
                "expected_application_id": expected_application_id,
                "expected_revision": expected_revision,
            })
            operation_ids.append(str(uuid4()))
        try:
            result = applications.execute_application_update_batch(
                self._db_path,
                self._user_id,
                operation_ids=operation_ids,
                client_turn_id=self._client_turn_id,
                commands=prepared,
                occurred_date=self._local_date,
            )
        except applications.ApplicationUpdateOperationNotFound:
            return ToolResponse.error(
                "这笔岗位修改操作不存在或不属于当前用户；本次没有写入。",
                data={"reason": "application_update_operation_not_found"},
            )
        except applications.ApplicationUpdateOperationConflict as error:
            return ToolResponse.error(
                str(error) or "这笔岗位修改与本轮已有操作收据冲突；本次没有再次写入。",
                data={"reason": "application_update_operation_conflict"},
            )
        except ValueError:
            return ToolResponse.error("岗位修改参数无效；本次没有写入。")

        if result.get("operation_type") != "application_update_batch":
            return ToolResponse.error("岗位批量修改返回了无法识别的结果；请查询看板确认当前状态。")
        if result.get("state") == "rejected":
            issues = result.get("issues") if isinstance(result.get("issues"), list) else []
            if (
                len(requested_updates) == 1
                and issues
                and issues[0].get("reason") == "merge_required"
            ):
                detail = issues[0].get("detail")
                if not isinstance(detail, dict):
                    return ToolResponse.error(
                        "岗位合并预览缺少可信目标；整批没有写入。", data=result,
                    )
                # Consume the request budget before a second lookup so failure cannot let
                # the model enumerate more targets in the same turn.
                self._request_proposal_write_fence.trip("application_merge")
                try:
                    recorder_kwargs = (
                        {"proposal_recorder": self._proposal_recorder}
                        if self._proposal_recorder is not None
                        else {}
                    )
                    proposal = applications.prepare_application_merge_operation(
                        self._db_path,
                        self._user_id,
                        source_application_id=detail["source_id"],
                        source_company=detail["source_company"],
                        source_position=detail["source_position"],
                        destination_application_id=detail["destination_id"],
                        destination_company=detail["destination_company"],
                        destination_position=detail["destination_position"],
                        **recorder_kwargs,
                    )
                except (applications.ApplicationMergeOperationConflict, ValueError) as error:
                    return ToolResponse.error(str(error) or "无法安全生成岗位合并预览")
                if proposal.get("status") == "not_found":
                    return ToolResponse.error(
                        "合并源或目标刚被其它操作修改；本次没有改写任何岗位，请刷新后重试。",
                        data=proposal,
                    )
                if proposal.get("status") == "same_application":
                    return ToolResponse.error(
                        "源岗位与保留岗位是同一条记录，无需合并。", data=proposal,
                    )
                source = proposal["source"]
                destination = proposal["destination"]
                requested_changes = prepared[0]["changes"]
                stage_note = (
                    "；本次同时请求的阶段改动尚未执行，合并后请单独修改阶段"
                    if (
                        "stage" in requested_changes
                        and requested_changes["stage"] != detail.get("source_stage")
                    )
                    else ""
                )
                return ToolResponse.ok(
                    f"已生成待确认合并预览：将移除 #{source['application_id']} "
                    f"{source['company']}·{source['position']}，并入保留的 "
                    f"#{destination['application_id']} {destination['company']}·"
                    f"{destination['position']}{stage_note}。当前尚未合并；请让用户逐项核对页面卡片"
                    "并点击按钮，自然语言确认不能代替批准。",
                    data=proposal,
                )
            summaries = []
            for issue in issues[:5]:
                item_index = issue.get("index")
                reason = issue.get("reason", "unknown")
                summaries.append(
                    f"第 {item_index + 1} 条：{reason}"
                    if isinstance(item_index, int)
                    else str(reason)
                )
            suffix = "；".join(summaries) or "安全校验未通过"
            return ToolResponse.partial(f"整批没有写入（{suffix}）。", data=result)

        if result.get("state") == "no_change":
            return ToolResponse.partial(
                f"这 {result['requested_count']} 条岗位已经是请求的值；"
                "本次没有写入，也没有创建新收据。",
                data=result,
            )
        if result.get("state") != "completed":
            return ToolResponse.error("岗位批量修改返回了未知状态；请查询看板确认当前记录。")
        no_change_count = result.get("no_change_count")
        suffix = f"，另有 {no_change_count} 条无需修改" if no_change_count else ""
        return ToolResponse.ok(
            f"已原子修改 {result.get('changed_count')} 条岗位{suffix}。"
            "页面已有每条可信操作收据；只有对应的页面撤销按钮能执行 Undo。",
            data=result,
        )


class DeleteApplicationTool(Tool):
    """Prepare one or more deletion proposals; the model can never execute deletion."""

    def __init__(self, db_path: str, user_id: str, *,
                 request_proposal_write_fence: RequestProposalWriteFence | None = None,
                 proposal_recorder: Callable[[Connection, str, str], object] | None = None):
        super().__init__(
            "delete_application",
            "为删除时间线里的一条或多条岗位记录生成精确待确认卡；本工具不会直接删除。"
            "仅在用户明确说「删掉/不要这条记录」时调用；"
            "「不投了/主动放弃/撤回申请」不是删除，用 update_application 改为 withdrawn；"
            "「被拒/挂了」改为 rejected。批准后岗位与历程会删除；题目、复盘原文、"
            "简历和拷打记录会保留但解除岗位绑定。自然语言确认不能代替页面按钮。"
            "删除明确的全部岗位时直接传 scope=all；删除其他多条岗位时用 targets 一次提交整批；"
            "每个用户请求最多尝试生成一个删除批次。",
            origin="careerdesk",
        )
        self._db_path = db_path
        self._user_id = user_id
        self._request_proposal_write_fence = (
            request_proposal_write_fence or RequestProposalWriteFence()
        )
        self._proposal_recorder = proposal_recorder

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("company", "string", "删除单条时传记录里的公司名", required=False),
            ToolParameter("position", "string", "记录里的岗位名（该公司只有一条记录时可省略）",
                          required=False),
            ToolParameter(
                "scope",
                "string",
                "用户明确要删除求职进展中全部岗位时传 all",
                required=False,
                schema={"type": "string", "enum": ["all"]},
            ),
            ToolParameter(
                "targets",
                "array",
                "删除多条时一次传完整目标列表",
                required=False,
                schema={
                    "description": (
                        "精确目标数组，每项仅含 company 和 position；一次 1–200 条，"
                        "不得重复。只用于删除多条，不能与 company/position 同传。"
                    ),
                },
            ),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        """Consume the request budget, then resolve and freeze without business writes."""
        blocked = self._request_proposal_write_fence.blocked_response()
        if blocked is not None:
            return blocked
        self._request_proposal_write_fence.trip("application_delete")
        if not isinstance(parameters, dict):
            return ToolResponse.error("删除岗位参数不符合安全契约")
        company = parameters.get("company")
        position = parameters.get("position")
        scope = parameters.get("scope")
        targets = parameters.get("targets")
        supplied_modes = sum((
            company is not None or position is not None,
            targets is not None,
            scope is not None,
        ))
        if supplied_modes != 1:
            return ToolResponse.error("删除单条、指定多条与全部岗位参数必须三选一")
        if scope is not None and scope != "all":
            return ToolResponse.error("scope 只能为 all")
        if targets is None and scope is None and company is None:
            return ToolResponse.error("删除单条岗位时必须提供公司名")
        try:
            if scope == "all":
                proposals = applications.prepare_all_application_delete_operations(
                    self._db_path,
                    self._user_id,
                    proposal_recorder=self._proposal_recorder,
                )
                if not proposals:
                    return ToolResponse.partial("当前求职进展没有岗位记录，无需删除。")
                return ToolResponse.ok(
                    f"已一次生成全部 {len(proposals)} 条待确认删除预览。当前尚未删除；"
                    "请让用户逐项核对同一批次，并用页面按钮一次处理全部。"
                    "自然语言确认不能代替批准。",
                    data={
                        "operation_type": "application_delete_batch",
                        "state": "pending",
                        "operations": proposals,
                    },
                )
            if targets is not None:
                proposals = applications.prepare_application_delete_operations(
                    self._db_path,
                    self._user_id,
                    targets,
                    proposal_recorder=self._proposal_recorder,
                )
                return ToolResponse.ok(
                    f"已一次生成 {len(proposals)} 条待确认删除预览。当前尚未删除；"
                    "请让用户逐项核对同一批次，并用页面按钮一次处理全部。"
                    "自然语言确认不能代替批准。",
                    data={
                        "operation_type": "application_delete_batch",
                        "state": "pending",
                        "operations": proposals,
                    },
                )
            proposal = applications.prepare_application_delete_operation(
                self._db_path,
                self._user_id,
                company=company,
                position=position,
                proposal_recorder=self._proposal_recorder,
            )
        except (applications.ApplicationDeleteOperationConflict, ValueError) as error:
            return ToolResponse.error(str(error) or "无法安全生成岗位删除预览")
        if proposal.get("status") == "not_found":
            label = "·".join(value for value in (company, position) if value)
            return ToolResponse.error(f"没找到精确匹配的岗位记录：{label}。")
        if proposal.get("status") == "ambiguous":
            options = " / ".join(proposal["options"])
            return ToolResponse.partial(
                f"{company} 有多条岗位记录（{options}），请向用户确认准确岗位名；"
                "下一轮带 position 再调用。",
                data=proposal,
            )
        target = proposal["target"]
        effect = proposal["effect"]
        return ToolResponse.ok(
            f"已生成待确认删除预览：{target['company']}·{target['position']}（岗位记录 "
            f"#{target['application_id']}，将删除 {len(effect['timeline_entries'])} 条历程；题目、复盘原文、"
            "简历和拷打记录保留但会解除岗位绑定）。当前尚未删除；请让用户核对页面卡片并"
            "点击按钮，自然语言确认不能代替批准。",
            data=proposal,
        )
