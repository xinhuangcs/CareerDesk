"""Explicit Agent commands stay bounded and reuse orchestration command seams."""

import asyncio
from types import SimpleNamespace

from careerdesk.agentic.tools.request_application_prep import RequestApplicationPrepTool


def run(coroutine):
    return asyncio.run(coroutine)


def test_request_application_prep_resolves_exact_target_and_maps_refresh(monkeypatch):
    from careerdesk.agentic.tools import request_application_prep as module

    calls = []
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(db_path="test.db"))
    monkeypatch.setattr(
        module,
        "find_applications_by_company",
        lambda *_args, **_kwargs: [{"id": 9, "company": "示例公司", "position": "后端"}],
    )

    async def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "started", "refresh_applied": True}

    monkeypatch.setattr(module, "request_prep_generation", request)
    result = run(RequestApplicationPrepTool("u1").arun({
        "company": "示例", "position": "后端", "action": "refresh",
    }))

    assert result.status == "success"
    assert calls[0][0][1:] == ("u1", 9)
    assert calls[0][1]["force"] is False
    assert calls[0][1]["refresh_research"] is True
    assert result.data["ui_actions"] == [
        {"kind": "open_application_research", "resource_id": 9},
    ]


def test_request_application_prep_is_one_attempt_per_turn(monkeypatch):
    from careerdesk.agentic.tools import request_application_prep as module

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(db_path="test.db"))
    monkeypatch.setattr(
        module,
        "find_applications_by_company",
        lambda *_args, **_kwargs: [
            {"id": 9, "company": "示例公司", "position": "后端"},
            {"id": 10, "company": "示例公司", "position": "前端"},
        ],
    )
    tool = RequestApplicationPrepTool("u1")
    first = run(tool.arun({"company": "示例", "action": "start"}))
    second = run(tool.arun({"company": "示例", "action": "start"}))
    assert first.status == "partial"
    assert second.status == "error"
    assert "已经尝试过一次" in second.text
