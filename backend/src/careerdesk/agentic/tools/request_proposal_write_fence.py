"""Request-scoped high-risk proposal fence for dependent write paths."""

from typing import Literal

from agentmaker import ToolResponse


ProposalType = Literal[
    "job_intake",
    "application_delete",
    "review_undo",
    "application_merge",
]


class RequestProposalWriteFence:
    """Monotonic open-to-closed proposal fence shared within one user request."""

    def __init__(self) -> None:
        self._proposal_type: ProposalType | None = None

    @property
    def tripped(self) -> bool:
        return self._proposal_type is not None

    @property
    def proposal_type(self) -> ProposalType | None:
        return self._proposal_type

    def trip(self, proposal_type: ProposalType) -> None:
        """Close dependent writes permanently before the first high-risk proposal attempt."""
        if self._proposal_type is None:
            self._proposal_type = proposal_type

    def blocked_response(self) -> ToolResponse | None:
        """Return uniform rejection when closed; readers and preferences do not use it."""
        if self._proposal_type is None:
            return None
        return ToolResponse.error(
            "本轮已经生成或尝试生成过一批高风险确认预览，不能再修改求职记录或创建另一批"
            "高风险确认卡，以免让刚冻结的依赖立即失效。查询与长期偏好仍可使用；其它求职记录"
            "修改请等待用户下一轮明确提出。",
            data={
                "reason": "request_proposal_write_fence",
                "proposal_type": self._proposal_type,
            },
        )
