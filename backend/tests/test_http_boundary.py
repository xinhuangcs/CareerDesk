
import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerdesk.platform.http.request_trust import (
    RequestTrustMiddleware,
    WRITE_REQUEST_HEADER,
)


WRITE_HEADERS = {WRITE_REQUEST_HEADER: "1"}


def _client(
    runtime_mode: str = "server",
    *,
    allowed_origins: tuple[str, ...] = ("https://jobs.example.com",),
    base_url: str = "http://testserver",
) -> TestClient:
    app = FastAPI()

    @app.api_route(
        "/api/write",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def api_write() -> dict:
        return {"ok": True}

    @app.post("/outside-api")
    async def outside_api() -> dict:
        return {"ok": True}

    app.add_middleware(
        RequestTrustMiddleware,
        runtime_mode=runtime_mode,
        allowed_origins=allowed_origins,
    )
    return TestClient(app, base_url=base_url)


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_strict_modes_require_custom_header_for_every_api_write(method):
    client = _client()

    rejected = getattr(client, method)("/api/write")
    accepted = getattr(client, method)("/api/write", headers=WRITE_HEADERS)

    assert rejected.status_code == 403
    body = rejected.json()
    assert rejected.headers["content-type"] == "application/problem+json"
    assert rejected.headers["x-request-id"] == body["request_id"]
    assert uuid.UUID(body["request_id"]).version == 4
    assert body["status"] == 403 and body["detail"] == "untrusted browser write request"
    assert accepted.status_code == 200


def test_test_mode_only_compatibly_allows_requests_without_browser_signals():
    client = _client("test", allowed_origins=("http://testserver",))

    assert client.post("/api/write").status_code == 200
    assert client.post(
        "/api/write",
        headers={"Origin": "http://testserver"},
    ).status_code == 403
    assert client.post(
        "/api/write",
        headers={**WRITE_HEADERS, "Origin": "http://testserver"},
    ).status_code == 200
    assert client.post(
        "/api/write",
        headers={WRITE_REQUEST_HEADER: "wrong"},
    ).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {**WRITE_HEADERS, "Sec-Fetch-Site": "cross-site"},
        {**WRITE_HEADERS, "Sec-Fetch-Site": "unexpected"},
        {**WRITE_HEADERS, "Sec-Fetch-Mode": "cors"},
        {**WRITE_HEADERS, "Sec-Fetch-Site": "same-site"},
        {**WRITE_HEADERS, "Sec-Fetch-Site": "none"},
    ],
)
def test_untrusted_or_incomplete_fetch_metadata_is_rejected(headers):
    assert _client().post("/api/write", headers=headers).status_code == 403


def test_same_origin_fetch_metadata_without_url_signal_is_allowed():
    response = _client().post(
        "/api/write",
        headers={**WRITE_HEADERS, "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        " https://jobs.example.com",
        "https://jobs.example.com/",
        "https://jobs.example.com/path",
        "https://jobs.example.com?next=evil",
        "https://jobs.example.com?",
        "https://jobs.example.com#",
        "https://jobs.example.com.evil",
        "https://jobs.example.com:444",
        "http://jobs.example.com",
        "ftp://jobs.example.com",
        "https://user@jobs.example.com",
    ],
)
def test_server_rejects_null_malformed_or_unlisted_origins(origin):
    response = _client().post(
        "/api/write",
        headers={**WRITE_HEADERS, "Origin": origin},
    )

    assert response.status_code == 403


def test_server_accepts_only_semantically_exact_allowlisted_origin():
    client = _client()

    implicit_port = client.post(
        "/api/write",
        headers={**WRITE_HEADERS, "Origin": "https://jobs.example.com"},
    )
    explicit_default_port = client.post(
        "/api/write",
        headers={**WRITE_HEADERS, "Origin": "https://jobs.example.com:443"},
    )

    assert implicit_port.status_code == explicit_default_port.status_code == 200


def test_cross_site_fetch_is_rejected_even_when_origin_is_allowlisted():
    response = _client().post(
        "/api/write",
        headers={
            **WRITE_HEADERS,
            "Origin": "https://jobs.example.com",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status_code == 403


def test_referer_is_used_only_as_fallback_to_origin():
    client = _client()

    trusted_referer = client.post(
        "/api/write",
        headers={**WRITE_HEADERS, "Referer": "https://jobs.example.com/page?tab=one"},
    )
    evil_referer = client.post(
        "/api/write",
        headers={**WRITE_HEADERS, "Referer": "https://evil.example/page"},
    )
    evil_origin_wins = client.post(
        "/api/write",
        headers={
            **WRITE_HEADERS,
            "Origin": "https://evil.example",
            "Referer": "https://jobs.example.com/page",
        },
    )

    assert trusted_referer.status_code == 200
    assert evil_referer.status_code == evil_origin_wins.status_code == 403


@pytest.mark.parametrize(
    ("base_url", "origin"),
    [
        ("http://localhost:8000", "http://localhost:5173"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:5173"),
        ("http://127.0.0.1:8000", "http://localhost:5173"),
    ],
)
@pytest.mark.parametrize("runtime_mode", ["desktop", "development"])
def test_local_modes_allow_same_loopback_host_on_a_different_port(
    runtime_mode,
    base_url,
    origin,
):
    client = _client(runtime_mode, allowed_origins=(), base_url=base_url)

    response = client.post(
        "/api/write",
        headers={**WRITE_HEADERS, "Origin": origin},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("base_url", "origin"),
    [
        ("http://localhost:8000", "http://localhost.evil:5173"),
        ("http://127.0.0.1:8000", "http://192.168.1.10:5173"),
        ("http://example.com", "http://example.com"),
    ],
)
def test_local_modes_do_not_expand_trust_beyond_the_validated_loopback_host(base_url, origin):
    client = _client("desktop", allowed_origins=(), base_url=base_url)

    response = client.post(
        "/api/write",
        headers={**WRITE_HEADERS, "Origin": origin},
    )

    assert response.status_code == 403


def test_safe_and_non_api_requests_are_not_subject_to_the_write_gate():
    client = _client()

    assert client.get("/api/write").status_code == 200
    assert client.options("/api/write").status_code == 200
    assert client.post("/outside-api").status_code == 200


def test_rejection_happens_before_downstream_or_body_receive():
    downstream_called = False
    receive_called = False
    sent: list[dict] = []

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        nonlocal receive_called
        receive_called = True
        raise AssertionError("rejected request body must not be read")

    async def send(message):
        sent.append(message)

    middleware = RequestTrustMiddleware(
        downstream,
        runtime_mode="server",
        allowed_origins=("https://jobs.example.com",),
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/uploads",
        "raw_path": b"/api/uploads",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"jobs.example.com"),
            (b"content-type", b"multipart/form-data; boundary=large"),
            (b"content-length", b"99999999"),
        ],
        "client": ("203.0.113.10", 54321),
        "server": ("127.0.0.1", 8000),
    }

    asyncio.run(middleware(scope, receive, send))

    assert downstream_called is False and receive_called is False
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 403


def test_invalid_middleware_configuration_fails_fast():
    async def downstream(scope, receive, send):
        return None

    with pytest.raises(ValueError, match="未知运行模式"):
        RequestTrustMiddleware(downstream, runtime_mode="production")
    with pytest.raises(ValueError, match="非法可信 Origin"):
        RequestTrustMiddleware(
            downstream,
            runtime_mode="server",
            allowed_origins=("https://jobs.example.com/path",),
        )
