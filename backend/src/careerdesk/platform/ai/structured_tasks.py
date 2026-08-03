"""Bounded execution primitives for one-shot structured AI calls.

This module owns only provider/framework-facing safety mechanics: conservative
window accounting, explicit output reservation, and bounded fresh
validation retry. Product prompts, corpus selection, persistence, deadlines,
and user-facing errors remain with their feature owners.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from agentmaker import (
    DEFAULT_PROMPTS,
    Agent,
    LLMResponseError,
    WindowBudgetConfig,
    count_tokens,
)

STRUCTURED_OUTPUT_MAX_WINDOW_FRACTION = 0.5
STRUCTURED_MIN_OUTPUT_TOKENS = 256
STRUCTURED_CONTEXT_GUARD_TOKENS = 256
STRUCTURED_MESSAGE_OVERHEAD_TOKENS = 128
STRUCTURED_RETRY_INSTRUCTION = (
    "上一次输出没有通过结构化校验。请重新生成；仍只返回符合 JSON Schema 的 JSON 本体，"
    "不要解释，不要用 markdown 代码块包裹，也不要复述上一次输出。"
)
STRUCTURED_RETRY_FEEDBACK_MAX_CHARS = 30
STRUCTURED_MAX_VALIDATION_RETRIES: Literal[2] = 2

INVALID_MODEL_CAPACITY = "invalid_model_capacity"
MISSING_MODEL_CAPACITY = "missing_model_capacity"
INSUFFICIENT_CONTEXT = "insufficient_context"
StructuredTaskCapacityReason = Literal[
    "invalid_model_capacity",
    "missing_model_capacity",
    "insufficient_context",
]


class StructuredTaskCapacityError(RuntimeError):
    """A framework-level capacity failure with a feature-neutral reason."""

    def __init__(self, reason: StructuredTaskCapacityReason) -> None:
        self.reason = reason
        super().__init__(reason)


class StructuredTaskValidationError(ValueError):
    """A model result failed deterministic validation after schema parsing.

    Only this explicit error type participates in the structured task's shared
    validation retry budget.  Validators must leave programming errors and
    cancellation untouched so they cannot be mistaken for model-output faults.
    """


def _positive_model_limit(llm, attribute: str) -> int | None:
    value = getattr(llm, attribute, None)
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise StructuredTaskCapacityError(INVALID_MODEL_CAPACITY)
    return value


def effective_context_window(llm) -> int:
    """Return a validated model window; missing metadata is not a safe estimate."""
    context_window = _positive_model_limit(llm, "context_window")
    if context_window is None:
        raise StructuredTaskCapacityError(MISSING_MODEL_CAPACITY)
    return context_window


def desired_output_tokens(llm, task_limit: int) -> int:
    """Clamp a positive task output limit to model and window capabilities."""
    if type(task_limit) is not int or task_limit <= 0:
        raise ValueError("task output limit must be a positive integer")
    context_window = effective_context_window(llm)
    provider_limit = _positive_model_limit(llm, "max_output_tokens")
    return WindowBudgetConfig(
        desired_output_tokens=task_limit,
        max_output_fraction=STRUCTURED_OUTPUT_MAX_WINDOW_FRACTION,
    ).output_reserve(
        window=context_window,
        model_max_output=provider_limit,
    )


def conservative_tokens(text: str) -> int:
    """Upper-bound common tokenizers with UTF-8 bytes and a heuristic backstop."""
    return max(2 * count_tokens(text), len(text.encode("utf-8")))


def structured_input_tokens(system_prompt: str, payload: str, schema_model) -> int:
    """Count prompt text plus a conservative provider-native schema copy."""
    schema = schema_model.model_json_schema()
    schema_json = json.dumps(schema, ensure_ascii=False)
    wire_schema_json = json.dumps(schema, ensure_ascii=True)
    schema_instruction = DEFAULT_PROMPTS.render(
        "harness.schema_instruction",
        schema=schema_json,
    )
    safe_input = (
        conservative_tokens(schema_instruction)
        + conservative_tokens(system_prompt)
        + conservative_tokens(payload)
        # Some SDK serializers escape non-ASCII schema metadata on the wire.
        # The ASCII form bounds both escaped and raw UTF-8 representations.
        + conservative_tokens(wire_schema_json)
    )
    return safe_input + STRUCTURED_MESSAGE_OVERHEAD_TOKENS


def _bounded_output_tokens(
    llm,
    task_limit: int,
    *,
    system_prompt: str,
    payload: str,
    schema_model,
) -> int:
    """Fit one fresh structured call inside input + output + guard budget."""
    context_window = effective_context_window(llm)
    input_tokens = structured_input_tokens(system_prompt, payload, schema_model)
    available = context_window - input_tokens - STRUCTURED_CONTEXT_GUARD_TOKENS
    output_tokens = min(desired_output_tokens(llm, task_limit), available)
    if output_tokens < STRUCTURED_MIN_OUTPUT_TOKENS:
        raise StructuredTaskCapacityError(INSUFFICIENT_CONTEXT)
    return output_tokens


async def run_structured_task(
    llm,
    *,
    name: str,
    system_prompt: str,
    payload: str,
    schema_model,
    task_output_limit: int,
    validation_retries: Literal[0, 1, 2],
    result_validator: Callable[[Any], Any] | None = None,
):
    """Run a bounded task with a shared schema/post-validation retry budget."""
    if (type(validation_retries) is not int
            or validation_retries not in range(STRUCTURED_MAX_VALIDATION_RETRIES + 1)):
        raise ValueError("validation_retries must be explicitly 0, 1 or 2")

    retry_payload = f"{payload}\n\n{STRUCTURED_RETRY_INSTRUCTION}"
    # The feedback form is deliberately no larger than the generic retry
    # instruction already included in capacity preflight.  Better diagnostics
    # therefore do not steal output tokens from unrelated structured tasks.
    budget_payload = retry_payload if validation_retries else payload
    validation_feedback: str | None = None
    for attempt in range(validation_retries + 1):
        attempt_payload = payload if attempt == 0 else retry_payload
        if attempt and validation_feedback is not None:
            attempt_payload = (
                f"{payload}\n\n校验失败：{validation_feedback}。"
                "只返回符合 JSON Schema 的 JSON；"
                "不要解释、markdown 或复述旧输出。"
            )
        try:
            # A new agent is the freshness boundary.  A provider-valid result
            # rejected by the post-validator has already entered the prior
            # agent's conversation, so reusing that agent would leak the bad
            # output into the retry even though the user payload is fresh.
            agent = Agent(name, llm, system_prompt=system_prompt)
            result = await agent.arun(
                attempt_payload,
                output_schema=schema_model,
                retries=0,
                max_tokens=_bounded_output_tokens(
                    llm,
                    task_output_limit,
                    system_prompt=system_prompt,
                    payload=budget_payload,
                    schema_model=schema_model,
                ),
            )
            output = result.final_output
            return result_validator(output) if result_validator is not None else output
        except (LLMResponseError, StructuredTaskValidationError) as error:
            if attempt == validation_retries:
                raise
            if isinstance(error, StructuredTaskValidationError):
                validation_feedback = " ".join(str(error).split())[
                    :STRUCTURED_RETRY_FEEDBACK_MAX_CHARS
                ]
            else:
                # Provider parse errors may contain rejected output or request
                # internals.  Keep their retry feedback deliberately generic.
                validation_feedback = "字段、类型、枚举、数量或长度不符合 JSON Schema"
    raise AssertionError("unreachable structured task retry state")


__all__ = [
    "INSUFFICIENT_CONTEXT",
    "INVALID_MODEL_CAPACITY",
    "MISSING_MODEL_CAPACITY",
    "STRUCTURED_CONTEXT_GUARD_TOKENS",
    "STRUCTURED_MESSAGE_OVERHEAD_TOKENS",
    "STRUCTURED_MAX_VALIDATION_RETRIES",
    "STRUCTURED_MIN_OUTPUT_TOKENS",
    "STRUCTURED_RETRY_INSTRUCTION",
    "STRUCTURED_RETRY_FEEDBACK_MAX_CHARS",
    "StructuredTaskCapacityError",
    "StructuredTaskValidationError",
    "conservative_tokens",
    "desired_output_tokens",
    "effective_context_window",
    "run_structured_task",
    "structured_input_tokens",
]
