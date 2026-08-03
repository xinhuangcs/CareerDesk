
import asyncio
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from careerdesk.core.config import get_settings
from careerdesk.platform.database import read_connection, transaction
from careerdesk.orchestration.assistant.contracts import ChatRequest
from careerdesk.orchestration.assistant.ledger import (
    chat_request_hash,
    claim_turn,
    complete_turn,
    mark_turn_unknown,
    record_proposal_operation,
)
from careerdesk.platform.http.request_trust import WRITE_REQUEST_HEADER


def _problem_body(response) -> dict:
    body = response.json()
    request_id = body.pop("request_id")
    assert response.headers["x-request-id"] == request_id
    assert uuid.UUID(request_id).version == 4
    assert response.headers["content-type"] == "application/problem+json"
    return body


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"
    assert uuid.UUID(r.headers["x-request-id"]).version == 4


_BUSINESS_TABLES = (
    "applications", "companies", "timeline_entries", "journal", "status_log",
    "questions", "question_knowledge", "knowledge_points",
    "review_question_occurrences", "resumes",
    "grill_sessions", "grill_answers", "preferences", "preference_owners",
    "preference_item_commands", "preference_item_command_owners",
    "application_update_undo_commands", "review_timeline_entry_edit_undo_commands",
    "application_intake_operation_owners",
)


def _iter_api_routes(routes):
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _iter_api_routes(included.routes)
        elif getattr(route, "methods", None) and getattr(route, "path", None):
            yield route
        elif getattr(route, "routes", None):
            yield from _iter_api_routes(route.routes)


def test_get_requests_never_write_business_tables(client, tmp_path):
    db_path = str(tmp_path / "careerdesk.db")

    def counts() -> dict[str, int]:
        with read_connection(db_path) as conn:
            return {
                table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in _BUSINESS_TABLES
            }

    get_paths = sorted({
        route.path
        for route in _iter_api_routes(client.app.routes)
        if "GET" in (route.methods or set()) and route.path.startswith(("/api", "/status"))
    })
    assert len(get_paths) >= 20, f"枚举到的 GET 路由异常少（{len(get_paths)}），测试可能失效"

    before = counts()
    for path in get_paths:
        client.get(re.sub(r"\{[^}]+\}", "1", path))
    assert counts() == before


def test_chat_request_strictly_validates_review_supplement_context(client):
    reference = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    payload = {
        "message": "是二面",
        "session_id": str(uuid.uuid4()),
        "client_turn_id": str(uuid.uuid4()),
        "review_supplement_reference": reference,
        "attachments": [{
            "kind": "document",
            "filename": "note.txt",
            "text": "补充材料",
        }],
    }

    request = ChatRequest.model_validate(payload)

    assert str(request.review_supplement_reference) == reference
    schema = ChatRequest.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["review_supplement_reference"]["anyOf"][0]["format"] == "uuid"
    assert schema["$defs"]["ChatAttachment"]["additionalProperties"] is False
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ChatRequest.model_validate({**payload, "hidden_context": "not trusted"})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ChatRequest.model_validate({
            **payload,
            "attachments": [{**payload["attachments"][0], "hidden_context": "not trusted"}],
        })
    with pytest.raises(ValidationError, match="规范 UUID"):
        ChatRequest.model_validate({
            **payload,
            "review_supplement_reference": reference.upper(),
        })
    assert client.post(
        "/api/chat",
        json={**payload, "hidden_context": "not trusted"},
    ).status_code == 422
    assert client.post(
        "/api/chat",
        json={
            **payload,
            "attachments": [{**payload["attachments"][0], "hidden_context": "not trusted"}],
        },
    ).status_code == 422


def test_composed_app_rejects_evil_host_and_cross_site_write_before_routes(client):
    evil_host = client.get("/healthz", headers={"Host": "localhost.evil"})
    cross_site = client.post(
        "/api/maintenance/reconcile",
        headers={
            WRITE_REQUEST_HEADER: "1",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert evil_host.status_code == 400
    assert cross_site.status_code == 403
    assert _problem_body(cross_site) == {
        "type": "about:blank",
        "title": "Forbidden",
        "status": 403,
            "detail": "untrusted browser write request",
            "code": "http_403",
            "params": {},
    }
    assert uuid.UUID(evil_host.headers["x-request-id"]).version == 4
    assert "access-control-allow-origin" not in cross_site.headers


def test_private_api_and_global_browser_security_headers_are_composed(client):
    api = client.get("/api/settings")
    page = client.get("/healthz")

    assert api.headers["cache-control"] == "no-store"
    assert api.headers["pragma"] == "no-cache"
    for response in (api, page):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_openapi_documents_required_browser_write_marker(client):
    schema = client.app.openapi()
    write_operation = schema["paths"]["/api/maintenance/reconcile"]["post"]
    read_operation = schema["paths"]["/api/settings"]["get"]

    marker = next(
        parameter for parameter in write_operation["parameters"]
        if parameter.get("name") == WRITE_REQUEST_HEADER
    )
    assert marker["in"] == "header" and marker["required"] is True
    assert marker["schema"]["enum"] == ["1"]
    assert marker["schema"]["default"] == "1"
    assert not any(
        parameter.get("name") == WRITE_REQUEST_HEADER
        for parameter in read_operation.get("parameters", [])
    )


def test_server_mode_is_fail_closed_at_composed_http_and_auth_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_RUNTIME_MODE", "server")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "server-data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    monkeypatch.setenv("APP_GATEWAY_AUTH_SECRET", "high-entropy-test-secret")
    monkeypatch.setenv("APP_DEV_FAKE_USER", "")
    monkeypatch.setenv("APP_ALLOWED_HOSTS", "jobs.example.com")
    monkeypatch.setenv("APP_ALLOWED_ORIGINS", "https://jobs.example.com")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    gateway = {
        "X-Gateway-Auth": "high-entropy-test-secret",
        "Remote-User": "alice",
    }
    trusted_write = {
        **gateway,
        WRITE_REQUEST_HEADER: "1",
        "Origin": "https://jobs.example.com",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        with TestClient(create_app(), base_url="https://jobs.example.com") as server:
            assert server.get("/api/settings").status_code == 401
            assert server.get("/api/settings", headers=gateway).status_code == 200
            assert server.post(
                "/api/maintenance/reconcile", headers=gateway,
            ).status_code == 403
            assert server.post(
                "/api/maintenance/reconcile", headers=trusted_write,
            ).status_code == 200
            assert server.get(
                "/healthz",
                headers={"Host": "evil.example", "X-Forwarded-Host": "jobs.example.com"},
            ).status_code == 400
    finally:
        get_settings.cache_clear()


def test_chat_sse_protocol(client):
    payload = {
        "message": "你好",
        "session_id": str(uuid.uuid4()),
        "client_turn_id": str(uuid.uuid4()),
    }
    with client.stream("POST", "/api/chat", json=payload) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: error" in body and "event: done" not in body
    data = next(
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines() if line.startswith("data: ")
    )
    assert data["code"] == "model_not_configured"
    assert data["history_committed"] is False and data["attachments"] == "retained"


def test_chat_sse_redacts_internal_errors(client, monkeypatch):
    async def fail(*_args, **_kwargs):
        if False:
            yield None
        raise RuntimeError("LEAKME /private/data/careerdesk.db provider-secret")

    monkeypatch.setattr("careerdesk.orchestration.assistant.service.stream_prepared_chat", fail)
    payload = {
        "message": "触发异常",
        "session_id": str(uuid.uuid4()),
        "client_turn_id": str(uuid.uuid4()),
    }
    with client.stream("POST", "/api/chat", json=payload) as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    assert "event: error" in body and "turn_outcome_unknown" in body
    assert "trace_id" in body and "LEAKME" not in body and "/private/data" not in body


def test_running_turn_is_rejected_before_sse_headers(client):
    payload = {
        "message": "还在跑",
        "session_id": str(uuid.uuid4()),
        "client_turn_id": str(uuid.uuid4()),
        "attachments": [],
    }
    digest = chat_request_hash(payload["session_id"], payload["message"], [])
    claimed = claim_turn(
        get_settings().db_path,
        get_settings().dev_fake_user or "me",
        payload["client_turn_id"],
        payload["session_id"],
        digest,
    )
    assert claimed.status == "execute"

    response = client.post("/api/chat", json=payload)
    assert response.status_code == 409 and response.headers["retry-after"] == "2"
    problem = _problem_body(response)
    assert problem == {
        "type": "urn:careerdesk:problem:chat-turn-rejected",
        "title": "Chat turn rejected",
        "status": 409,
        "detail": "这一轮仍在处理中，请稍等片刻后用同一请求重试。",
        "code": "turn_in_progress",
        "params": {},
        "retryable": True,
        "attachments": "retained",
        "client_turn_id": payload["client_turn_id"],
    }


def test_prepare_chat_carries_review_supplement_reference_and_rejects_hash_drift(client):
    from careerdesk.orchestration.assistant import service

    session_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    first_reference = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    second_reference = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
    user_id = get_settings().dev_fake_user or "me"
    prepared = service.prepare_chat(
        "是二面",
        session_id,
        turn_id,
        user_id,
        review_supplement_reference=first_reference,
        agent_factory=lambda _hooks: object(),
    )

    assert prepared.mode == "execute"
    assert prepared.review_supplement_reference == first_reference
    with pytest.raises(service.ChatTurnRejected) as drifted:
        service.prepare_chat(
            "是二面",
            session_id,
            turn_id,
            user_id,
            review_supplement_reference=second_reference,
            agent_factory=lambda _hooks: object(),
        )
    assert drifted.value.data["code"] == "idempotency_key_reused"
    service.abandon_prepared_chat(prepared)


def test_chat_turn_status_is_tenant_scoped_and_terminal_is_derived(client):
    user_id = get_settings().dev_fake_user or "me"
    session_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    path = f"/api/chat/turns/{turn_id}/status"

    assert client.get(path).json() == {
        "client_turn_id": turn_id,
        "state": "absent",
        "terminal": False,
        "proposal_operations": [],
    }
    claimed = claim_turn(
        get_settings().db_path,
        user_id,
        turn_id,
        session_id,
        chat_request_hash(session_id, "执行", []),
    )
    assert client.get(path).json() == {
        "client_turn_id": turn_id,
        "state": "running",
        "terminal": False,
        "proposal_operations": [],
    }
    assert client.get(path, headers={"Remote-User": "other"}).json() == {
        "client_turn_id": turn_id,
        "state": "absent",
        "terminal": False,
        "proposal_operations": [],
    }

    complete_turn(
        get_settings().db_path,
        user_id,
        turn_id,
        claimed.attempt_token,
        [
            {"event": "message_snapshot", "data": {"text": "完成"}},
            {"event": "done", "data": {
                "session": session_id,
                "message_id": str(uuid.uuid4()),
                "client_turn_id": turn_id,
                "history_committed": True,
                "attachments": "consumed",
                "replayed": True,
            }},
        ],
    )
    assert client.get(path).json() == {
        "client_turn_id": turn_id,
        "state": "completed",
        "terminal": True,
        "proposal_operations": [],
    }

    unknown_session = str(uuid.uuid4())
    unknown_turn = str(uuid.uuid4())
    unknown = claim_turn(
        get_settings().db_path,
        user_id,
        unknown_turn,
        unknown_session,
        chat_request_hash(unknown_session, "不确定", []),
    )
    assert mark_turn_unknown(
        get_settings().db_path,
        user_id,
        unknown_turn,
        unknown.attempt_token,
        {"code": "turn_outcome_unknown", "message": "未确认", "retryable": False},
    )
    assert client.get(f"/api/chat/turns/{unknown_turn}/status").json() == {
        "client_turn_id": unknown_turn,
        "state": "unknown",
        "terminal": True,
        "proposal_operations": [],
    }
    assert client.get("/api/chat/turns/not-a-uuid/status").status_code == 422


def test_chat_turn_status_has_explicit_openapi_contract(client):
    openapi = client.app.openapi()
    operation = openapi["paths"][
        "/api/chat/turns/{client_turn_id}/status"
    ]["get"]
    response_schema = operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/ChatTurnStatusResponse"}

    path_parameter = next(
        parameter for parameter in operation["parameters"]
        if parameter["name"] == "client_turn_id"
    )
    assert path_parameter["in"] == "path" and path_parameter["required"] is True
    assert path_parameter["schema"]["format"] == "uuid"

    schema = openapi["components"]["schemas"]["ChatTurnStatusResponse"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "client_turn_id", "state", "terminal", "proposal_operations",
    ]
    assert schema["properties"]["client_turn_id"]["format"] == "uuid"
    assert schema["properties"]["state"]["enum"] == [
        "absent", "running", "completed", "unknown", "cancelled",
    ]
    assert schema["properties"]["terminal"]["type"] == "boolean"
    assert schema["properties"]["proposal_operations"]["maxItems"] == 200
    proposal_items = schema["properties"]["proposal_operations"]["items"]
    assert proposal_items == {
        "$ref": "#/components/schemas/ChatProposalOperationReference",
    }
    proposal_schema = openapi["components"]["schemas"][
        "ChatProposalOperationReference"
    ]
    assert proposal_schema["additionalProperties"] is False
    assert proposal_schema["required"] == ["surface", "operation_id"]
    assert proposal_schema["properties"]["operation_id"]["format"] == "uuid"


def test_chat_recovery_scope_is_stable_tenant_scoped_and_never_cached(client):
    with transaction(get_settings().db_path) as conn:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='assistant_recovery_scope_secret'",
            ("00" * 32,),
        )
    alice_headers = {"Remote-User": "alice@example.com"}
    alice = client.get("/api/chat/recovery-scope", headers=alice_headers)
    replay = client.get("/api/chat/recovery-scope", headers=alice_headers)
    bob = client.get(
        "/api/chat/recovery-scope",
        headers={"Remote-User": "bob@example.com"},
    )

    expected = "bfbccb935753dab130375d47312c5a10e04f45408fe0ce38bdd18f9ecc563cc4"
    assert alice.status_code == 200
    assert alice.json() == {"scope": expected}
    assert replay.json() == alice.json()
    assert bob.json()["scope"] != expected
    assert alice.json()["scope"] != (
        "c5ffa31f91656da23864cc74b0c31e37b1dc845516e6634c6338771511728b6d"
    )
    assert "alice@example.com" not in alice.text
    assert alice.headers["cache-control"] == "no-store"
    assert alice.headers["pragma"] == "no-cache"


def test_chat_recovery_scope_hashes_exact_unicode_identity_and_rejects_empty(client):
    from careerdesk.orchestration.assistant.service import get_chat_recovery_scope

    with transaction(get_settings().db_path) as conn:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='assistant_recovery_scope_secret'",
            ("00" * 32,),
        )
    response = get_chat_recovery_scope("Å丽丝")
    assert response.model_dump() == {
        "scope": "d48bc7249dbc9d0cad968eb8f8cc1f1794f44e3accc2b5a90cebf5669849e97f",
    }
    with pytest.raises(ValueError, match="user_id must be non-empty"):
        get_chat_recovery_scope("")


def test_chat_recovery_scope_fails_closed_if_instance_secret_is_invalid(client):
    from careerdesk.orchestration.assistant.service import get_chat_recovery_scope

    with transaction(get_settings().db_path) as conn:
        conn.execute(
            "UPDATE meta SET value='not-a-secret' "
            "WHERE key='assistant_recovery_scope_secret'",
        )
    with pytest.raises(RuntimeError, match="secret is missing or invalid"):
        get_chat_recovery_scope("alice")


def test_chat_recovery_scope_has_explicit_openapi_contract(client):
    openapi = client.app.openapi()
    operation = openapi["paths"]["/api/chat/recovery-scope"]["get"]
    response_schema = operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/ChatRecoveryScopeResponse"}

    schema = openapi["components"]["schemas"]["ChatRecoveryScopeResponse"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["scope"]
    assert schema["properties"]["scope"] == {
        "type": "string",
        "maxLength": 64,
        "minLength": 64,
        "pattern": "^[0-9a-f]{64}$",
        "title": "Scope",
    }


def test_cancel_absent_chat_turn_is_terminal_tenant_scoped_and_fences_late_post(client):
    user_id = get_settings().dev_fake_user or "me"
    turn_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    path = f"/api/chat/turns/{turn_id}/cancel-if-absent"

    cancelled = client.post(path, json={})
    assert cancelled.status_code == 200
    assert cancelled.json() == {
        "client_turn_id": turn_id,
        "state": "cancelled",
        "terminal": True,
        "proposal_operations": [],
    }
    assert client.post(path, json={}).json() == cancelled.json()
    assert client.post(path, json={"unexpected": True}).status_code == 422
    assert client.get(f"/api/chat/turns/{turn_id}/status").json() == cancelled.json()
    assert client.get(
        f"/api/chat/turns/{turn_id}/status",
        headers={"Remote-User": "other"},
    ).json() == {
        "client_turn_id": turn_id,
        "state": "absent",
        "terminal": False,
        "proposal_operations": [],
    }

    late = client.post(
        "/api/chat",
        json={
            "message": "迟到请求不得执行",
            "session_id": session_id,
            "client_turn_id": turn_id,
        },
    )
    assert late.status_code == 409
    assert _problem_body(late) == {
        "type": "urn:careerdesk:problem:chat-turn-rejected",
        "title": "Chat turn rejected",
        "status": 409,
        "detail": "这一轮已被安全取消；请使用新的请求编号重新发送。",
        "code": "turn_cancelled",
        "params": {},
        "retryable": False,
        "attachments": "retained",
        "client_turn_id": turn_id,
        "history_committed": False,
    }
    with read_connection(get_settings().db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM assistant_turns "
            "WHERE user_id=? AND client_turn_id=?",
            (user_id, turn_id),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM assistant_turn_cancellations "
            "WHERE user_id=? AND client_turn_id=?",
            (user_id, turn_id),
        ).fetchone() == (1,)
    assert client.post("/api/chat/turns/not-a-uuid/cancel-if-absent").status_code == 422


def test_cancel_if_absent_reports_existing_turn_without_changing_it(client):
    user_id = get_settings().dev_fake_user or "me"
    turn_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    claimed = claim_turn(
        get_settings().db_path,
        user_id,
        turn_id,
        session_id,
        chat_request_hash(session_id, "运行中", []),
    )
    operation_id = str(uuid.uuid4())
    record_proposal_operation(
        get_settings().db_path,
        user_id,
        turn_id,
        claimed.attempt_token,
        "application_delete",
        operation_id,
    )

    response = client.post(
        f"/api/chat/turns/{turn_id}/cancel-if-absent",
        json={},
    )
    assert response.json() == {
        "client_turn_id": turn_id,
        "state": "running",
        "terminal": False,
        "proposal_operations": [{
            "surface": "application_delete",
            "operation_id": operation_id,
        }],
    }
    with read_connection(get_settings().db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM assistant_turn_cancellations "
            "WHERE user_id=? AND client_turn_id=?",
            (user_id, turn_id),
        ).fetchone() == (0,)


def test_cancel_if_absent_has_explicit_openapi_contract(client):
    openapi = client.app.openapi()
    operation = openapi["paths"][
        "/api/chat/turns/{client_turn_id}/cancel-if-absent"
    ]["post"]
    response_schema = operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/ChatTurnStatusResponse"}

    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema == {"$ref": "#/components/schemas/ChatTurnCancelRequest"}
    command_schema = openapi["components"]["schemas"]["ChatTurnCancelRequest"]
    assert command_schema["additionalProperties"] is False
    assert "required" not in command_schema

    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert parameters["client_turn_id"]["in"] == "path"
    assert parameters["client_turn_id"]["required"] is True
    assert parameters["client_turn_id"]["schema"]["format"] == "uuid"
    assert parameters[WRITE_REQUEST_HEADER]["in"] == "header"
    assert parameters[WRITE_REQUEST_HEADER]["required"] is True

    active_cancel = openapi["paths"][
        "/api/chat/turns/{client_turn_id}/cancel"
    ]["post"]
    assert active_cancel["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ChatTurnCancelRequest",
    }
    assert active_cancel["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ChatTurnStatusResponse",
    }


def test_disconnect_before_response_headers_closes_claimed_turn(client):
    from careerdesk.orchestration.assistant import api, service

    session_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    prepared = service.prepare_chat(
        "响应头前断线",
        session_id,
        turn_id,
        get_settings().dev_fake_user or "me",
        agent_factory=lambda _hooks: object(),
    )
    assert prepared.mode == "execute"

    async def body():
        yield "never reached"

    async def receive():
        return {"type": "http.disconnect"}

    async def disconnect_on_headers(message):
        if message["type"] == "http.response.start":
            raise OSError("client gone")

    response = api._ClaimedEventSourceResponse(body(), prepared)
    with pytest.raises(ClientDisconnect):
        asyncio.run(response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            receive,
            disconnect_on_headers,
        ))

    with read_connection(get_settings().db_path) as conn:
        state = conn.execute(
            "SELECT state FROM assistant_turns WHERE user_id=? AND client_turn_id=?",
            (get_settings().dev_fake_user or "me", turn_id),
        ).fetchone()
    assert state == ("unknown",)


def test_disconnect_during_body_closes_agent_and_queue_waiter(client):
    from careerdesk.orchestration.assistant import api, service

    session_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    user_id = get_settings().dev_fake_user or "me"

    async def scenario():
        finalized = asyncio.Event()
        release = asyncio.Event()

        class Agent:
            async def astream_run(self, _payload, *, scope):
                try:
                    yield "这是足够长的首个流式响应块"
                    await release.wait()
                finally:
                    finalized.set()

        prepared = service.prepare_chat(
            "流中断线",
            session_id,
            turn_id,
            user_id,
            agent_factory=lambda _hooks: Agent(),
        )
        response = api._ClaimedEventSourceResponse(
            api._encode_prepared_events(prepared),
            prepared,
        )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def disconnect_on_body(message):
            if message["type"] == "http.response.body" and message.get("body"):
                release.set()
                raise OSError("client gone")

        with pytest.raises(ClientDisconnect):
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}},
                receive,
                disconnect_on_body,
            )
        await asyncio.wait_for(finalized.wait(), timeout=1)
        await asyncio.sleep(0)
        assert [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and getattr(task.get_coro(), "__qualname__", "") == "Queue.get"
        ] == []

    asyncio.run(scenario())
    with read_connection(get_settings().db_path) as conn:
        state = conn.execute(
            "SELECT state FROM assistant_turns WHERE user_id=? AND client_turn_id=?",
            (user_id, turn_id),
        ).fetchone()
    assert state == ("unknown",)


def test_sse_sends_model_delta_before_generation_finishes(client):
    from careerdesk.orchestration.assistant import api, service

    async def scenario():
        allow_finish = asyncio.Event()
        generation_finished = asyncio.Event()

        class Agent:
            async def astream_run(self, _payload, *, scope):
                yield "这是模型尚未生成完成时发送的首段回答。"
                await allow_finish.wait()
                generation_finished.set()
                yield "这是第二段回答。"

        prepared = service.prepare_chat(
            "给我一些面试建议",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            get_settings().dev_fake_user or "me",
            agent_factory=lambda _hooks: Agent(),
        )
        response = api._ClaimedEventSourceResponse(
            api._encode_prepared_events(prepared),
            prepared,
        )
        delta_bodies = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            body = message.get("body", b"")
            if message["type"] == "http.response.body" and b"event: message_delta" in body:
                delta_bodies.append(body)
                if not allow_finish.is_set():
                    assert not generation_finished.is_set()
                    allow_finish.set()

        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            receive,
            send,
        )
        assert generation_finished.is_set()
        assert len(delta_bodies) >= 2

    asyncio.run(scenario())


def test_response_disconnect_keeps_fence_while_agent_resists_cancel(client):
    from careerdesk.orchestration.assistant import api, service

    session_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    user_id = get_settings().dev_fake_user or "me"

    async def scenario():
        disconnected = asyncio.Event()
        release = asyncio.Event()

        class Agent:
            async def astream_run(self, _payload, *, scope):
                yield "这是足够长的首个流式响应块"
                await release.wait()

        prepared = service.prepare_chat(
            "响应流中断",
            session_id,
            turn_id,
            user_id,
            agent_factory=lambda _hooks: Agent(),
        )
        response = api._ClaimedEventSourceResponse(
            api._encode_prepared_events(prepared),
            prepared,
        )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def disconnect_on_body(message):
            if message["type"] == "http.response.body" and message.get("body"):
                disconnected.set()
                raise OSError("client gone")

        delivery = asyncio.create_task(response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            receive,
            disconnect_on_body,
        ))
        await asyncio.wait_for(disconnected.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not delivery.done()

        with read_connection(get_settings().db_path) as conn:
            state = conn.execute(
                "SELECT state FROM assistant_turns WHERE user_id=? AND client_turn_id=?",
                (user_id, turn_id),
            ).fetchone()
        assert state == ("running",)

        with pytest.raises(service.ChatTurnRejected) as blocked:
            service.prepare_chat(
                "不应并发的新一轮",
                session_id,
                str(uuid.uuid4()),
                user_id,
                agent_factory=lambda _hooks: object(),
            )
        assert blocked.value.data["code"] == "session_busy"

        release.set()
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(delivery, timeout=1)

    asyncio.run(scenario())
    with read_connection(get_settings().db_path) as conn:
        state = conn.execute(
            "SELECT state FROM assistant_turns WHERE user_id=? AND client_turn_id=?",
            (user_id, turn_id),
        ).fetchone()
    assert state == ("unknown",)
