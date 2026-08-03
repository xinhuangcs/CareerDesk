"""Request ID and RFC 9457 error-boundary regressions."""

from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from careerdesk.platform.http.problem_details import (
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    install_problem_details_handlers,
)


class _ValidatedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int


def _app() -> FastAPI:
    app = FastAPI()
    install_problem_details_handlers(app)

    @app.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    @app.get("/conflict")
    async def conflict() -> None:
        raise HTTPException(
            status_code=409,
            detail="当前状态冲突，请刷新后重试。",
            headers={"Retry-After": "3"},
        )

    @app.post("/validated")
    async def validated(_body: _ValidatedBody) -> dict:
        return {"ok": True}

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("provider-secret /private/careerdesk.db")

    app.add_middleware(RequestIdMiddleware)
    return app


def _assert_request_id(response) -> str:
    request_id = response.headers[REQUEST_ID_HEADER]
    parsed = UUID(request_id)
    assert parsed.version == 4 and str(parsed) == request_id
    return request_id


def test_success_responses_get_distinct_server_owned_request_ids():
    with TestClient(_app()) as client:
        first = client.get("/ok", headers={REQUEST_ID_HEADER: "attacker-controlled"})
        second = client.get("/ok")

    first_id = _assert_request_id(first)
    second_id = _assert_request_id(second)
    assert first.json() == second.json() == {"ok": True}
    assert first_id != "attacker-controlled" and first_id != second_id


def test_http_exception_uses_problem_json_and_preserves_protocol_headers():
    with TestClient(_app()) as client:
        response = client.get("/conflict")

    request_id = _assert_request_id(response)
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "3"
    assert response.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": 409,
        "code": "http_409",
        "params": {},
        "detail": "当前状态冲突，请刷新后重试。",
        "request_id": request_id,
    }


def test_validation_errors_keep_actionable_items_as_an_extension():
    with TestClient(_app()) as client:
        response = client.post(
            "/validated",
            json={"count": "not-an-integer", "unexpected": True},
        )

    request_id = _assert_request_id(response)
    body = response.json()
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert body["type"] == "urn:careerdesk:problem:request-validation"
    assert body["title"] == "Request validation failed"
    assert body["status"] == 422
    assert body["code"] == "request_validation"
    assert body["params"] == {}
    assert body["detail"] == "请求参数校验失败。"
    assert body["request_id"] == request_id
    assert isinstance(body["errors"], list) and len(body["errors"]) == 2
    assert all(set(item) == {"code", "loc"} for item in body["errors"])
    assert "not-an-integer" not in response.text
    assert {tuple(item["loc"]) for item in body["errors"]} == {
        ("body", "count"),
        ("body", "unexpected"),
    }


def test_unknown_errors_are_sanitized_but_still_traceable(caplog):
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/crash")

    request_id = _assert_request_id(response)
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "code": "internal_error",
        "params": {},
        "detail": "服务器处理请求时发生错误。",
        "request_id": request_id,
    }
    assert "provider-secret" not in response.text
    assert "/private/careerdesk.db" not in response.text
    assert request_id in caplog.text and "RuntimeError" in caplog.text
    assert "provider-secret" not in caplog.text and "/private/careerdesk.db" not in caplog.text
