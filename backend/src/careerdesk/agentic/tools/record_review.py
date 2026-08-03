"""Prepare review confirmation proposals with page-bound supplement targets."""

from uuid import UUID, uuid4

from agentmaker import Tool, ToolParameter, ToolResponse

from ...features.reviews.public import (
    MAX_REVIEW_RECORD_SOURCE_CHARS,
    ReviewExtractionUnavailable,
    ReviewRecordOperationConflict,
    ReviewRecordOperationNotFound,
    ReviewService,
)
from ...platform.locale import OutputLocale
from .request_proposal_write_fence import RequestProposalWriteFence

def _starts_as_existing_review_correction(text: str) -> bool:
    compact = "".join(text.split())
    return compact.startswith((
        "说错了",
        "我说错了",
        "刚才说错了",
        "之前说错了",
        "昨天说错了",
        "记错了",
        "我记错了",
        "刚才记错了",
        "之前记错了",
        "昨天记错了",
    ))


class RecordReviewTool(Tool):
    """Record real job-search progress with at most one write attempt per request."""

    def __init__(self, service: ReviewService, user_id: str, *,
                 client_turn_id: str | UUID,
                 review_supplement_reference: str | UUID | None = None,
                 trusted_source_text: str | None = None,
                 allow_batch: bool = False,
                 output_locale: OutputLocale = "zh-CN",
                 request_proposal_write_fence: RequestProposalWriteFence | None = None):
        self._output_locale = output_locale
        try:
            canonical_turn = str(UUID(str(client_turn_id)))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("client_turn_id 必须是 UUID") from error
        if isinstance(client_turn_id, str) and client_turn_id != canonical_turn:
            raise ValueError("client_turn_id 必须是规范的小写 UUID")
        bound_reference: str | None = None
        if review_supplement_reference is not None:
            try:
                bound_reference = str(UUID(str(review_supplement_reference)))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("review_supplement_reference 必须是 UUID") from error
            if (
                isinstance(review_supplement_reference, str)
                and review_supplement_reference != bound_reference
            ):
                raise ValueError("review_supplement_reference 必须是规范的小写 UUID")
        if bound_reference is not None and trusted_source_text is not None:
            supplement_guidance = (
                "This turn is a page-bound review supplement; its target and complete current addition are system-bound."
            )
        elif bound_reference is not None:
            supplement_guidance = (
                "This turn is a page-bound review supplement. Pass only the user's exact addition in text; the target is system-bound."
            )
        else:
            supplement_guidance = (
                "Never guess an opaque pending-review reference. The page binds it when the user chooses to add details; "
                "an unrelated new update may still be recorded."
            )
        super().__init__(
            "record_review",
            "Prepare confirmation proposals for real or newly confirmed applications, tests, interviews, outcomes, "
            "invitations, and appointments. Call exactly once with no arguments when the current request is system-bound; "
            "the system supplies the complete original message and isolates every role. Never use update_application for these "
            f"narrated events. Use parse_jobs for imports and manage_review to correct existing events. {supplement_guidance}",
            origin="careerdesk",
        )
        self._service = service
        self._user_id = user_id
        self._client_turn_id = canonical_turn
        self._review_supplement_reference = bound_reference
        self._trusted_source_text = trusted_source_text
        self._allow_batch = allow_batch and bound_reference is None
        self._attempt_consumed = False
        self._operation_id: str | None = None
        self._request_proposal_write_fence = (
            request_proposal_write_fence or RequestProposalWriteFence()
        )

    def get_parameters(self) -> list[ToolParameter]:
        if self._trusted_source_text is not None:
            return []
        return [
            ToolParameter("text", "string", "The user's exact review narration, without paraphrasing or omission."),
        ]

    def _l(self, zh: str, en: str) -> str:
        return en if self._output_locale == "en" else zh

    def _skipped_batch_note(self, skipped: list) -> str:
        """Name every role that failed extraction so none is dropped silently."""
        if not skipped:
            return ""
        names = self._l("；", "; ").join(
            " · ".join(filter(None, [identity.company, identity.position]))
            or self._l("未识别的岗位", "an unidentified role")
            for identity in skipped
        )
        return self._l(
            f"另有 {len(skipped)} 个岗位没能整理出来（{names}），这几条没有写入，"
            "请让用户单独再说一次。",
            f" {len(skipped)} role(s) could not be processed ({names}) and were not written; "
            "ask the user to narrate those separately.",
        )

    async def arun(self, parameters: dict) -> ToolResponse:
        """Execute asynchronously because extraction awaits the model."""
        blocked = self._request_proposal_write_fence.blocked_response()
        if blocked is not None:
            return blocked
        # Registries are request-scoped. Consume the budget before parsing or I/O; with no
        # await here, only one concurrent call in the event loop can cross this fence.
        if self._attempt_consumed:
            return ToolResponse.error(self._l(
                "本轮 record_review 已尝试过一次，不能再次写入；岗位清单请用 parse_jobs，其余请在下一轮继续。",
                "record_review already attempted a write in this turn. Use parse_jobs for role lists, or continue in the next turn.",
            ),
                data={"reason": "single_write_budget_exhausted"},
            )
        self._attempt_consumed = True

        expected_parameters = set() if self._trusted_source_text is not None else {"text"}
        if set(parameters) != expected_parameters:
            return ToolResponse.error(self._l(
                "record_review 的原文与补充目标必须由当前请求可信绑定。",
                "record_review source text and supplement targets must be bound by the current request.",
            ),
                data={"reason": "untrusted_review_parameters"},
            )

        text = (
            self._trusted_source_text
            if self._trusted_source_text is not None
            else parameters.get("text")
        )
        if (not isinstance(text, str) or not text.strip()
                or len(text) > MAX_REVIEW_RECORD_SOURCE_CHARS):
            return ToolResponse.error(self._l(
                f"text 必须是非空字符串，且不能超过 {MAX_REVIEW_RECORD_SOURCE_CHARS:,} 个字符。",
                f"text must be nonempty and no longer than {MAX_REVIEW_RECORD_SOURCE_CHARS:,} characters.",
            ),
                data={"reason": "invalid_review_text"},
            )
        if (
            self._review_supplement_reference is None
            and _starts_as_existing_review_correction(text)
        ):
            return ToolResponse.error(self._l(
                "这句话是在纠正已有复盘，请改用 manage_review 修正原历程。",
                "This corrects an existing review. Use manage_review to edit the original event.",
            ),
                data={"reason": "review_correction_requires_edit"},
            )
        review_reference = self._review_supplement_reference

        if self._allow_batch:
            try:
                outcome = await self._service.execute_batch_record_operations(
                    self._user_id,
                    client_turn_id=self._client_turn_id,
                    text=text,
                )
            except (ReviewRecordOperationNotFound, ReviewRecordOperationConflict):
                return ToolResponse.error(self._l(
                    "批量复盘与当前记录状态有冲突；已生成的方案仍会显示，请逐项核对后再处理。",
                    "The review batch conflicts with current state. Existing proposals remain visible for review.",
                ),
                    data={"reason": "batch_review_record_conflict"},
                )
            except ReviewExtractionUnavailable as error:
                messages = {
                    "timeout": self._l(
                        "这批进展的整理超过了本轮时限，已整批取消且没有生成或写入岗位方案；请重试。",
                        "This batch exceeded its processing deadline. The whole batch was cancelled and no role proposals were written; try again.",
                    ),
                    "provider_request": self._l(
                        "模型服务请求失败，这批进展已整批取消且没有生成或写入岗位方案；请稍后重试。",
                        "The model request failed. The whole batch was cancelled and no role proposals were written; try again shortly.",
                    ),
                    "invalid_response": self._l(
                        "模型返回没有通过批量进展结构校验，已整批取消且没有生成或写入岗位方案；请重试。",
                        "The model response failed batch-progress validation. The whole batch was cancelled and no role proposals were written; try again.",
                    ),
                }
                failure_kind = (
                    error.reason if error.reason in messages else "invalid_response"
                )
                return ToolResponse.error(
                    messages[failure_kind],
                    data={
                        "reason": "batch_review_extraction_unavailable",
                        "failure_kind": failure_kind,
                        "failure_phase": (
                            "identity" if error.phase == "batch_identity" else "item"
                        ),
                    },
                )
            except ValueError:
                return ToolResponse.error(self._l(
                    "这次复盘无法安全拆成互不混写的岗位方案；其余内容尚未发布。",
                    "This review could not be safely split into isolated role proposals; remaining content was not published.",
                ),
                    data={"reason": "batch_review_record_validation_failed"},
                )
            results = outcome.results
            skipped_note = self._skipped_batch_note(outcome.skipped)
            identities = []
            missing_count = 0
            for result in results:
                preview = result.get("preview") if isinstance(result, dict) else None
                extraction = preview.get("extraction") if isinstance(preview, dict) else None
                missing = preview.get("missing") if isinstance(preview, dict) else None
                if isinstance(missing, list) and missing:
                    missing_count += 1
                if isinstance(extraction, dict):
                    identities.append(" · ".join(filter(None, [
                        extraction.get("company"), extraction.get("position"),
                    ])) or self._l("岗位信息待补充", "Role details needed"))
            count = len(results)
            operation_ids = [result["operation_id"] for result in results]
            if count == 1:
                identity = identities[0] if identities else self._l("岗位信息待补充", "Role details needed")
                message = (
                    self._l(
                        f"已为你准备 {identity} 的复盘草稿，但尚不能安全定位岗位。请在下方编辑或排除后统一确认。",
                        f"A review draft is ready for {identity}, but the role cannot yet be identified safely. Edit or exclude it below, then confirm the batch.",
                    )
                    if missing_count
                    else self._l(
                        f"已为你准备 {identity} 的复盘方案。你可以编辑或排除，再统一确认。",
                        f"A review proposal is ready for {identity}. Edit or exclude it, then confirm once.",
                    )
                )
                return ToolResponse.partial(
                    message + skipped_note,
                    data={
                        "status": "batch_preview",
                        "operation_id": operation_ids[0],
                        "operation_ids": operation_ids,
                        "skipped_count": len(outcome.skipped),
                    },
                )
            visible_identities = self._l("；", "; ").join(identities[:5])
            if len(identities) > 5:
                visible_identities += self._l(f"；等共 {len(identities)} 个岗位", f"; {len(identities)} roles total")
            missing_note = (
                self._l(f"其中 {missing_count} 条仍需可选补充岗位身份；", f"{missing_count} still need optional role details. ")
                if missing_count else ""
            )
            return ToolResponse.partial(
                self._l(
                    f"已拆成 {count} 条独立复盘方案（{visible_identities}）。{missing_note}请逐条核对后统一确认。",
                    f"This was split into {count} independent review proposals ({visible_identities}). {missing_note}Review each, then confirm once.",
                ) + skipped_note,
                data={
                    "status": "batch_preview",
                    "operation_ids": operation_ids,
                    "skipped_count": len(outcome.skipped),
                },
            )

        self._operation_id = str(uuid4())
        try:
            result = await self._service.execute_record_operation(
                self._user_id,
                operation_id=self._operation_id,
                client_turn_id=self._client_turn_id,
                text=text,
                review_reference=review_reference,
            )
        except ReviewRecordOperationNotFound:
            return ToolResponse.error(self._l(
                "待补充的复盘引用不存在或不属于当前用户，本次没有写入。",
                "The review supplement target is missing or belongs to another user; nothing was written.",
            ),
                data={"reason": "review_record_operation_not_found"},
            )
        except ReviewRecordOperationConflict:
            return ToolResponse.error(self._l(
                "这条复盘与当前记录状态有冲突，本次没有重复写入。请以页面中的最新方案为准。",
                "This review conflicts with current state and was not written again. Use the latest page proposal.",
            ),
                data={"reason": "review_record_operation_conflict"},
            )
        except ReviewExtractionUnavailable as error:
            messages = {
                "timeout": self._l(
                    "这条进展的整理超过了本轮时限；原话已保留，但没有写入岗位、时间线或题库，请重试。",
                    "This update exceeded its processing deadline. The source was retained, but no role, timeline, or question-bank data was written; try again.",
                ),
                "provider_request": self._l(
                    "模型服务请求失败；原话已保留，但没有写入岗位、时间线或题库，请稍后重试。",
                    "The model request failed. The source was retained, but no role, timeline, or question-bank data was written; try again shortly.",
                ),
                "invalid_response": self._l(
                    "模型返回没有通过进展结构校验；原话已保留，但没有写入岗位、时间线或题库，请重试。",
                    "The model response failed progress validation. The source was retained, but no role, timeline, or question-bank data was written; try again.",
                ),
            }
            failure_kind = error.reason if error.reason in messages else "invalid_response"
            return ToolResponse.error(
                messages[failure_kind],
                data={
                    "reason": "review_extraction_unavailable",
                    "failure_kind": failure_kind,
                },
            )
        except ValueError:
            # Pydantic/LLM validation errors may embed input fragments and internal URLs.
            # The durable operation receipt is already sanitized; never re-expose raw details.
            return ToolResponse.error(self._l(
                "这次复盘整理没有通过安全校验；原话已保留，未写入岗位、时间线或题库。",
                "This review failed safety validation. The source text was retained, but no role, timeline, or question-bank data was written.",
            ),
                data={"reason": "review_record_validation_failed"},
            )

        if result["state"] == "processing":
            return ToolResponse.partial(self._l(
                "复盘原话已安全保存，仍在整理中；页面会继续核对同一请求。",
                "The review text was saved safely and is still being processed; the page will keep checking this request.",
            ),
                data=result,
            )
        if result["state"] == "pending_confirmation":
            preview = result["preview"]
            extraction = preview["extraction"]
            identity = " · ".join(filter(None, [
                extraction.get("company"),
                extraction.get("position"),
            ])) or self._l("岗位信息待补充", "Role details needed")
            if preview["missing"]:
                message = self._l(
                    f"已为你准备 {identity} 的复盘草稿，但尚不能安全定位岗位。"
                    "你可以在下方方案直接编辑或排除，再统一确认；"
                    "当前不会发布时间线或题库。",
                    f"A review draft is ready for {identity}, but the role cannot yet be identified safely. Edit or exclude it below, then confirm. Nothing has been published.",
                )
            else:
                message = self._l(
                    f"已为你准备 {identity} 的复盘方案。你可以直接编辑或排除，"
                    "再统一确认一次。未提供的环节、日期等内容会保持为空。",
                    f"A review proposal is ready for {identity}. Edit or exclude it, then confirm once; omitted details remain empty.",
                )
            return ToolResponse.partial(message, data=result)
        if result["state"] == "completed" and result["outcome"] == "needs_clarification":
            receipt = result["result"]
            asks = "；".join(item["ask"] for item in receipt["missing"])
            follow_up = self._l(
                (
                "（拿到回答后请让用户继续回复，页面会重新绑定这条待补充复盘。）"
                if self._review_supplement_reference is not None
                else "（页面会把这些问题显示成可选补充；用户不补充也可以继续记录"
                "其它岗位，选择补充时页面会可信绑定到这一条。）"
                ),
                (
                    " Ask the user to continue; the page will rebind this review."
                    if self._review_supplement_reference is not None
                    else " The page shows these as optional details and binds the target only if the user chooses to answer."
                ),
            )
            return ToolResponse.partial(
                self._l(f"信息不全，请向用户追问：{asks}{follow_up}", f"More detail is needed: {asks}.{follow_up}"),
                data=result,
            )
        if result["state"] == "superseded":
            return ToolResponse.error(self._l(
                "这次补充已被更新的补充取代；请以页面中的最新复盘状态为准。",
                "A newer supplement replaced this one. Use the latest review state on the page.",
            ),
                data=result,
            )
        if result["state"] == "failed":
            return ToolResponse.error(self._l(
                "复盘原话已保存，但这次整理没有完成，也没有写入岗位、时间线或题库。",
                "The review text was saved, but processing did not finish and no role, timeline, or question-bank data was written.",
            ),
                data=result,
            )
        if result["state"] != "completed" or result["outcome"] != "applied":
            return ToolResponse.error(self._l("复盘操作返回了无法识别的状态。", "The review operation returned an unknown state."), data=result)

        receipt = result["result"]
        extraction = receipt["extraction"]
        derivation = receipt["derivation"]
        application = receipt["application"]
        history = extraction.get("history") or {}
        projected = extraction.get("projected_state") or {}
        next_action = extraction.get("next_action") or {}
        parts = [self._l(
            f"已记录：{application['company']}·{application['position']} 的求职进展",
            f"Recorded job-search progress for {application['company']} · {application['position']}",
        )]
        if history.get("step"):
            parts.append(self._l(f"本次环节 {history['step']}", f"event: {history['step']}"))
        if projected.get("stage"):
            parts.append(self._l(f"阶段更新为 {projected['stage']}", f"stage: {projected['stage']}"))
        if projected.get("current_step"):
            parts.append(self._l(f"当前环节更新为 {projected['current_step']}", f"current step: {projected['current_step']}"))
        if next_action.get("step"):
            parts.append(self._l(f"下一步 {next_action['step']}", f"next: {next_action['step']}"))
        if derivation["question_ids"]:
            parts.append(self._l(f"真题入库 {len(derivation['question_ids'])} 道", f"{len(derivation['question_ids'])} interview questions saved"))
        if derivation["knowledge_point_ids"]:
            parts.append(self._l(f"涉及知识点 {len(derivation['knowledge_point_ids'])} 个", f"{len(derivation['knowledge_point_ids'])} knowledge points linked"))
        if derivation["status_log_ids"]:
            parts.append(self._l("状态日志已记", "status log saved"))
        return ToolResponse.ok(self._l("，", "; ").join(parts) + self._l("。", "."), data=result)
