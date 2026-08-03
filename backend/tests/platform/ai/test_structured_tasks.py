"""Shared structured-task window and fresh-retry safety contracts."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agentmaker import DEFAULT_PROMPTS, LLMResponseError, count_tokens
from agentmaker.testing import ScriptedLLM
from pydantic import BaseModel, ConfigDict, Field

from careerdesk.platform.ai.structured_tasks import (
    INSUFFICIENT_CONTEXT,
    INVALID_MODEL_CAPACITY,
    MISSING_MODEL_CAPACITY,
    STRUCTURED_MESSAGE_OVERHEAD_TOKENS,
    StructuredTaskCapacityError,
    StructuredTaskValidationError,
    conservative_tokens,
    desired_output_tokens,
    effective_context_window,
    run_structured_task,
    structured_input_tokens,
)


class _StructuredResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(
        min_length=1,
        max_length=80,
        description="带非 ASCII 内容的结构化结果",
    )


class _CapturingScriptedLLM(ScriptedLLM):
    def __init__(
        self,
        *responses: dict,
        context_window: int | None = 8_192,
        max_output_tokens: int | None = 8_192,
    ) -> None:
        super().__init__(
            [json.dumps(response, ensure_ascii=False) for response in responses],
            context_window=context_window,
        )
        self.max_output_tokens = max_output_tokens
        self.chat_messages: list[list[dict]] = []
        self.chat_kwargs: list[dict] = []

    async def chat(self, messages, *, tools=None, **kwargs):
        self.chat_messages.append(messages)
        self.chat_kwargs.append(kwargs)
        return await super().chat(messages, tools=tools, **kwargs)


def _run(coroutine):
    return asyncio.run(coroutine)


def _task(llm, *, validation_retries: int):
    return run_structured_task(
        llm,
        name="结构化测试任务",
        system_prompt="只返回 schema。",
        payload="不可信输入数据。",
        schema_model=_StructuredResult,
        task_output_limit=512,
        validation_retries=validation_retries,
    )


def test_native_schema_budget_covers_ascii_escaped_wire_representation():
    schema = _StructuredResult.model_json_schema()
    prompt_schema = json.dumps(schema, ensure_ascii=False)
    wire_schema = json.dumps(schema, ensure_ascii=True)
    schema_instruction = DEFAULT_PROMPTS.render(
        "harness.schema_instruction",
        schema=prompt_schema,
    )

    assert len(wire_schema.encode("utf-8")) > len(prompt_schema.encode("utf-8"))
    assert structured_input_tokens("", "", _StructuredResult) == (
        conservative_tokens(schema_instruction)
        + conservative_tokens("")
        + conservative_tokens("")
        + conservative_tokens(wire_schema)
        + STRUCTURED_MESSAGE_OVERHEAD_TOKENS
    )


@pytest.mark.parametrize(
    "text",
    [
        "字" * 1_000,
        "😀" * 1_000,
        "".join(chr(33 + ((index * 47) % 90)) for index in range(4_000)),
    ],
    ids=["cjk", "emoji", "high-entropy-ascii"],
)
def test_conservative_token_bound_covers_utf8_and_heuristic_counts(text):
    assert conservative_tokens(text) == max(
        2 * count_tokens(text),
        len(text.encode("utf-8")),
    )


def test_unknown_model_context_fails_instead_of_guessing_a_window():
    llm = SimpleNamespace(context_window=None, max_output_tokens=None)

    with pytest.raises(StructuredTaskCapacityError) as captured:
        effective_context_window(llm)

    assert captured.value.reason == MISSING_MODEL_CAPACITY


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("context_window", 0),
        ("context_window", True),
        ("max_output_tokens", 0),
        ("max_output_tokens", True),
    ],
)
def test_invalid_capacity_metadata_has_a_stable_generic_reason(attribute, value):
    llm = SimpleNamespace(context_window=8_192, max_output_tokens=8_192)
    setattr(llm, attribute, value)

    with pytest.raises(StructuredTaskCapacityError) as captured:
        desired_output_tokens(llm, 512)

    assert captured.value.reason == INVALID_MODEL_CAPACITY
    assert str(captured.value) == INVALID_MODEL_CAPACITY


def test_tiny_context_fails_before_any_model_call_with_generic_reason():
    llm = _CapturingScriptedLLM(
        {"answer": "不会被读取"},
        context_window=1,
        max_output_tokens=1,
    )

    with pytest.raises(StructuredTaskCapacityError) as captured:
        _run(_task(llm, validation_retries=1))

    assert captured.value.reason == INSUFFICIENT_CONTEXT
    assert llm.chat_messages == []


def test_one_validation_retry_is_fresh_bounded_and_does_not_echo_bad_output():
    canary = "invalid-output-canary-must-not-enter-retry"
    llm = _CapturingScriptedLLM(
        {"answer": "first", "unexpected": canary},
        {"answer": "second"},
    )

    result = _run(_task(llm, validation_retries=1))

    assert result == _StructuredResult(answer="second")
    assert len(llm.chat_messages) == 2
    retry_request = json.dumps(llm.chat_messages[1], ensure_ascii=False)
    assert canary not in retry_request
    assert "校验失败：" in retry_request
    assert [message["role"] for message in llm.chat_messages[1]] == [
        "system",
        "system",
        "user",
    ]
    assert llm.chat_kwargs[0]["max_tokens"] == llm.chat_kwargs[1]["max_tokens"]


def test_post_schema_validation_failure_uses_the_same_fresh_retry():
    llm = _CapturingScriptedLLM(
        {"answer": "host-invalid-canary"},
        {"answer": "host-valid"},
    )

    def validate(result: _StructuredResult) -> _StructuredResult:
        if result.answer != "host-valid":
            raise StructuredTaskValidationError("host validation failed")
        return result

    result = _run(run_structured_task(
        llm,
        name="结构化测试任务",
        system_prompt="只返回 schema。",
        payload="不可信输入数据。",
        schema_model=_StructuredResult,
        task_output_limit=512,
        validation_retries=1,
        result_validator=validate,
    ))

    assert result.answer == "host-valid"
    assert len(llm.chat_messages) == 2
    retry_request = json.dumps(llm.chat_messages[1], ensure_ascii=False)
    assert "host-invalid-canary" not in retry_request
    assert "校验失败：host validation failed" in retry_request


def test_schema_retry_uses_generic_feedback_without_echoing_provider_error():
    canary = "provider-rejected-output-canary"
    llm = _CapturingScriptedLLM(
        {"answer": "first", "unexpected": canary},
        {"answer": "second"},
    )

    result = _run(_task(llm, validation_retries=1))

    assert result.answer == "second"
    retry_request = json.dumps(llm.chat_messages[1], ensure_ascii=False)
    assert canary not in retry_request
    assert "不符合 JSON Schema" in retry_request


def test_schema_and_post_validation_share_two_attempts_total():
    llm = _CapturingScriptedLLM(
        {"answer": "schema-invalid", "unexpected": "bad"},
        {"answer": "host-invalid"},
        {"answer": "must-not-be-called"},
    )

    def reject(_result: _StructuredResult) -> _StructuredResult:
        raise StructuredTaskValidationError("host validation failed")

    with pytest.raises(StructuredTaskValidationError, match="host validation failed"):
        _run(run_structured_task(
            llm,
            name="结构化测试任务",
            system_prompt="只返回 schema。",
            payload="不可信输入数据。",
            schema_model=_StructuredResult,
            task_output_limit=512,
            validation_retries=1,
            result_validator=reject,
        ))

    assert len(llm.chat_messages) == 2


def test_explicit_two_retry_budget_allows_three_fresh_attempts():
    llm = _CapturingScriptedLLM(
        {"answer": "first", "unexpected": "bad-1"},
        {"answer": "second", "unexpected": "bad-2"},
        {"answer": "third"},
    )

    result = _run(_task(llm, validation_retries=2))

    assert result.answer == "third"
    assert len(llm.chat_messages) == 3
    assert llm.chat_kwargs[0]["max_tokens"] == llm.chat_kwargs[2]["max_tokens"]


def test_post_validator_programming_errors_are_not_retried():
    llm = _CapturingScriptedLLM(
        {"answer": "valid-schema"},
        {"answer": "must-not-be-called"},
    )

    def broken(_result: _StructuredResult) -> _StructuredResult:
        raise RuntimeError("validator bug")

    with pytest.raises(RuntimeError, match="validator bug"):
        _run(run_structured_task(
            llm,
            name="结构化测试任务",
            system_prompt="只返回 schema。",
            payload="不可信输入数据。",
            schema_model=_StructuredResult,
            task_output_limit=512,
            validation_retries=1,
            result_validator=broken,
        ))

    assert len(llm.chat_messages) == 1


def test_zero_validation_retries_makes_exactly_one_attempt():
    llm = _CapturingScriptedLLM(
        {"answer": "first", "unexpected": "invalid"},
        {"answer": "second"},
    )

    with pytest.raises(LLMResponseError):
        _run(_task(llm, validation_retries=0))

    assert len(llm.chat_messages) == 1


@pytest.mark.parametrize("validation_retries", [-1, 3, True, None])
def test_validation_retry_count_only_accepts_explicit_zero_one_or_two(validation_retries):
    llm = _CapturingScriptedLLM({"answer": "unused"})

    with pytest.raises(ValueError, match="explicitly 0, 1 or 2"):
        _run(_task(llm, validation_retries=validation_retries))

    assert llm.chat_messages == []


def test_validation_retry_argument_has_no_implicit_default():
    llm = _CapturingScriptedLLM({"answer": "unused"})

    with pytest.raises(TypeError, match="validation_retries"):
        run_structured_task(
            llm,
            name="结构化测试任务",
            system_prompt="只返回 schema。",
            payload="不可信输入数据。",
            schema_model=_StructuredResult,
            task_output_limit=512,
        )

    assert llm.chat_messages == []
