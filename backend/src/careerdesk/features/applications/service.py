"""Batch application pipeline from source journaling through trusted page confirmation.

parse_batch creates previews and server operation IDs only; trusted HTTP approval is the
sole persistence trigger. The model client is duck-typed like ReviewService.
"""

import asyncio
from collections.abc import Callable
from sqlite3 import Connection

from agentmaker import Agent

from ...core.config import local_today
from . import repository
from .intake_models import BatchParse, merge_duplicate_positions
from .intake_models import ParsedPosition

PARSE_PROMPT = """你是岗位信息批量解析器。用户粘贴的内容可能是完整 JD、招聘页复制文、或口头列表（「投了字节和腾讯」），你把它拆成结构化岗位条目。输出直接进投递看板——字段错了看板就脏。今天是 {today}。

提取铁律：
- company 和 position 都必须有用户原文依据；任一项无法确定就丢弃该条，绝不猜测或补全。公司名用通用简称（字节跳动/腾讯），不带「有限公司」等后缀。
- 同一公司多个不同岗位拆成多条；完全相同的公司+岗位不要重复输出。
- 一次最多 200 条，宁可丢弃无法确定公司+岗位的噪声，不要凑数。

stage 映射举例（只在用户明确说出阶段时填写，没说或者不确定就留 null）：
- 想投/待投/待定等 → backlog
- 已投/已申请等 → applied
- 笔试等 → written_test
- 面试/二面/HR面等 → interviewing
- 拿到offer/被录取等 → offer
- 主动放弃/撤回/不再跟进等 → withdrawn
- 被拒/挂了等 → rejected
- 人才池等 → pooled

字段规则：
- 输入若是 CareerDesk 本地读取的表格结构化文本，每条必须设置 source_kind=workbook，并把“工作表名!行号”原样写入 source_row；普通粘贴文本保持 source_kind=text、source_row=null。表格中的备注、星标、泡池原因只有单元格明确表达时才填写，含义不确定就留空。
- 日期换算成 YYYY-MM-DD；说了「已投」但没说日期 → stage=applied、applied_date=null，保留阶段但绝不猜日期。
- current_step 是当前阶段下的具体环节（如「二面」「在线测评」），只记录用户已明确说明的现状。
- next_action 是唯一紧接着要做的事，使用 {{stage, step, date, time, note}}；不能确定具体 step 就留 null，不猜测。
- jd_text：普通文本必须原样截取，并返回它在完整输入中的零基 start/end（Python 切片语义）；无法逐字符验证就将三者都留空。表格输入可原样使用该行 JD 单元格，两个 offset 留空。skills 提炼明确要求的技能关键词（≤8 个短词，好：「RAG」「Kubernetes」；坏：「相关技术」「良好基础」）；highlights 记加分项。

返回前自检：每条的 company/position 是否都有原文依据；stage/current_step/next_action 是否都有用户原话支持；有无重复条目。"""


class ApplicationService:
    """Batch parsing and trusted-operation orchestration boundary."""

    def __init__(
        self,
        db_path: str,
        llm,
        *,
        proposal_recorder: Callable[[Connection, str, str], object] | None = None,
    ):
        self._db_path = db_path
        self._llm = llm
        self._proposal_recorder = proposal_recorder

    async def parse_batch(self, user_id: str, text: str, *, today: str | None = None) -> dict:
        """Parse a batch paste into a preview without business-table writes."""
        today = today or local_today().isoformat()
        journal_id, operation_id = repository.create_intake_batch(
            self._db_path, user_id, text,
        )
        agent = Agent("岗位解析器", self._llm, system_prompt=PARSE_PROMPT.format(today=today))
        try:
            result = await agent.arun(text, output_schema=BatchParse)
            parsed: BatchParse = result.final_output
            for position in parsed.positions:
                if position.jd_text is None or position.source_kind == "workbook":
                    continue
                start = position.jd_source_start
                end = position.jd_source_end
                if start is None or end is None or end > len(text) or text[start:end] != position.jd_text:
                    raise ValueError("模型返回的 JD source span 无法由用户原文逐字符验证")
            parsed_positions = merge_duplicate_positions(parsed.positions)
        except asyncio.CancelledError:
            repository.fail_intake_batch(
                self._db_path, user_id, journal_id, reason="parse_cancelled",
            )
            raise
        except Exception:
            repository.fail_intake_batch(self._db_path, user_id, journal_id)
            raise
        if not parsed_positions:
            repository.fail_intake_batch(
                self._db_path, user_id, journal_id, reason="empty_parse",
            )
            return {"status": "empty", "journal_id": journal_id,
                    "operation_id": operation_id}
        try:
            positions = repository.activate_intake_proposal(
                self._db_path,
                user_id,
                journal_id,
                parsed_positions,
                proposal_recorder=self._proposal_recorder,
            )
        except Exception:
            repository.fail_intake_batch(
                self._db_path, user_id, journal_id, reason="proposal_failed",
            )
            raise
        if positions is None:
            return {"status": "superseded", "journal_id": journal_id,
                    "operation_id": operation_id}
        return {"status": "preview", "journal_id": journal_id,
                "operation_id": operation_id, "positions": positions}

    def parse_standard_positions(
        self,
        user_id: str,
        positions: list[ParsedPosition],
        *,
        source_label: str,
        source_rows: int = 0,
        skipped_rows: int = 0,
    ) -> dict:
        """Deterministic standard-workbook path producing the same trusted proposal."""
        journal_id, operation_id = repository.create_intake_batch(
            self._db_path, user_id, source_label,
        )
        merged = merge_duplicate_positions(positions)
        if not merged:
            repository.fail_intake_batch(
                self._db_path, user_id, journal_id, reason="empty_standard_workbook",
            )
            return {
                "status": "empty", "journal_id": journal_id,
                "operation_id": operation_id, "source_rows": source_rows,
                "skipped_rows": skipped_rows,
            }
        try:
            planned = repository.activate_intake_proposal(
                self._db_path,
                user_id,
                journal_id,
                merged,
                source_rows=source_rows,
                skipped_rows=skipped_rows,
                proposal_recorder=self._proposal_recorder,
            )
        except Exception:
            repository.fail_intake_batch(
                self._db_path, user_id, journal_id, reason="proposal_failed",
            )
            raise
        if planned is None:
            return {"status": "superseded", "journal_id": journal_id,
                    "operation_id": operation_id}
        return {
            "status": "preview", "journal_id": journal_id,
            "operation_id": operation_id, "positions": planned,
            "source_rows": source_rows, "skipped_rows": skipped_rows,
        }

    def list_pending_intake_operations(self, user_id: str) -> list[dict]:
        return repository.list_pending_intake_operations(self._db_path, user_id)

    def get_intake_operation(self, user_id: str, operation_id: str) -> dict | None:
        return repository.get_intake_operation(self._db_path, user_id, operation_id)

    def approve_intake_operation(self, user_id: str, operation_id: str, *,
                                 exclude_indexes: list[int] | None = None) -> dict:
        return repository.approve_intake_operation(
            self._db_path, user_id, operation_id, exclude_indexes=exclude_indexes,
        )

    def reject_intake_operation(self, user_id: str, operation_id: str) -> dict:
        return repository.reject_intake_operation(self._db_path, user_id, operation_id)
