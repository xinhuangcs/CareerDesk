"""Interview-generation task error and retry boundaries."""

import asyncio

import pytest
from agentmaker import LLMRequestError

from careerdesk.features.questions.generation_models import GeneratedQuestionSet
from careerdesk.orchestration.interview_generation import ai_tasks
from careerdesk.platform.ai.structured_tasks import (
    INSUFFICIENT_CONTEXT,
    StructuredTaskCapacityError,
    StructuredTaskValidationError,
)


def run(coroutine):
    return asyncio.run(coroutine)


def _generated_question_set(*, ref_id: str = "R1") -> GeneratedQuestionSet:
    return GeneratedQuestionSet.model_validate({
        "questions": [{
            "text": "请说明你的相关经验",
            "category": "resume_deep_dive",
            "channel": "interview",
            "response_format": "oral_text",
            "difficulty": "introductory",
            "basis_kinds": ["resume"],
            "evidence_refs": [{"basis_kind": "resume", "ref_id": ref_id}],
            "limitations": [],
            "primary_competency": "经验表达",
            "secondary_tags": [],
            "evaluation_kind": "evidence_consistency",
            "rubric": {
                "essential_criteria": ["说明具体经验"],
                "quality_signals": [],
                "critical_errors": [],
            },
            "answer_guide": "说明背景、行动和结果。",
            "follow_up_allowed": True,
        }],
        "coverage": {
            "processed_sources": ["resume"],
            "covered_categories": ["resume_deep_dive"],
            "omitted_categories": [],
            "omission_reasons": [],
            "limitations": [],
        },
    })


def test_question_evidence_is_validated_inside_the_structured_retry_boundary():
    envelope = {"materials": [{
        "kind": "resume",
        "segments": [{"id": "R1", "text": "有效材料"}],
    }]}

    valid = _generated_question_set()
    assert ai_tasks._validate_question_evidence(valid, envelope) is valid
    with pytest.raises(StructuredTaskValidationError, match="evidence_refs"):
        ai_tasks._validate_question_evidence(_generated_question_set(ref_id="R2"), envelope)


def test_question_count_has_no_artificial_minimum_and_still_enforces_the_limit():
    envelope = {
        "effective_question_limit": 1,
        "materials": [{"kind": "resume", "segments": [{"id": "R1", "text": "有效材料"}]}],
    }

    one = _generated_question_set()
    assert ai_tasks._validate_question_evidence(one, envelope) is one
    too_many = one.model_copy(update={"questions": one.questions * 2})
    with pytest.raises(StructuredTaskValidationError, match="不能超过 1"):
        ai_tasks._validate_question_evidence(too_many, envelope)


def test_generation_uses_a_supported_structured_retry_budget(monkeypatch):
    captured = {}

    async def succeed(*_args, **kwargs):
        captured.update(kwargs)
        return _generated_question_set()

    monkeypatch.setattr(ai_tasks, "run_structured_task", succeed)
    run(ai_tasks.generate_question_set(object(), {}))

    assert captured["validation_retries"] == ai_tasks.STRUCTURED_MAX_VALIDATION_RETRIES == 2


@pytest.mark.parametrize("operation,args", [
    (ai_tasks.generate_question_set, ({},)),
    (ai_tasks.summarize_material, ({},)),
])
def test_capacity_failure_maps_to_one_safe_code(monkeypatch, operation, args):
    async def fail(*_args, **_kwargs):
        raise StructuredTaskCapacityError(INSUFFICIENT_CONTEXT)

    monkeypatch.setattr(ai_tasks, "run_structured_task", fail)
    with pytest.raises(ai_tasks.InterviewAITaskError, match="insufficient_model_capacity"):
        run(operation(object(), *args))


def test_provider_failure_does_not_expose_provider_detail(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise LLMRequestError("SECRET_PROVIDER_DETAIL", provider="test", model="test", status_code=401)

    monkeypatch.setattr(ai_tasks, "run_structured_task", fail)
    with pytest.raises(ai_tasks.InterviewAITaskError, match="model_request_failed") as caught:
        run(ai_tasks.generate_question_set(object(), {}))
    assert "SECRET_PROVIDER_DETAIL" not in str(caught.value)


def test_task_wrapper_does_not_swallow_cancellation(monkeypatch):
    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(ai_tasks, "run_structured_task", cancel)
    with pytest.raises(asyncio.CancelledError):
        run(ai_tasks.generate_question_set(object(), {}))


def test_unclassified_transport_failure_is_not_misclassified(monkeypatch):
    class TransportFailure(RuntimeError):
        pass

    async def fail(*_args, **_kwargs):
        raise TransportFailure("connection closed")

    monkeypatch.setattr(ai_tasks, "run_structured_task", fail)
    with pytest.raises(TransportFailure, match="connection closed"):
        run(ai_tasks.generate_question_set(object(), {}))
