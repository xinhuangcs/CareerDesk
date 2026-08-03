"""Public HTTP success responses must remain useful generated-client contracts."""

from fastapi.testclient import TestClient

from careerdesk.bootstrap.app import app


HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _success_responses(schema: dict):
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            for status, response in operation.get("responses", {}).items():
                if str(status).startswith("2"):
                    yield method.upper(), path, str(status), response


def test_openapi_success_responses_have_explicit_media_and_schema() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    responses = list(_success_responses(schema))

    assert len(responses) >= 70
    for method, path, status, response in responses:
        content = response.get("content") or {}
        label = f"{method} {path} -> {status}"
        if path == "/api/chat":
            assert content == {"text/event-stream": {"schema": {"type": "string"}}}, label
            continue
        if path == "/healthz":
            assert content == {"text/plain": {"schema": {"type": "string"}}}, label
            continue

        assert set(content) == {"application/json"}, label
        response_schema = content["application/json"].get("schema")
        assert response_schema, label
        assert response_schema.get("additionalProperties") is not True, label


def test_openapi_documents_problem_details_for_every_error_path() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    expected = {
        "description": "RFC 9457 Problem Details",
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"},
            },
        },
    }

    operations = 0
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operations += 1
            responses = operation["responses"]
            assert responses["default"] == expected
            if "422" in responses:
                assert responses["422"] == expected

    assert operations >= 70
    problem = schema["components"]["schemas"]["ProblemDetails"]
    assert set(problem["required"]) == {"type", "title", "status", "code", "request_id"}
    assert problem["additionalProperties"] is True
