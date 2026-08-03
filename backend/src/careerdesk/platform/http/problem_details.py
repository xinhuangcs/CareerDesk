"""Shared HTTP error contract and per-request trace identifiers."""

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
VALIDATION_PROBLEM_TYPE = "urn:careerdesk:problem:request-validation"
_RESERVED_MEMBERS = frozenset({"type", "title", "status", "detail", "request_id", "code", "params"})


class ProblemValidationItem(BaseModel):
    """Public validation location and stable code; never includes raw input or context."""

    model_config = ConfigDict(extra="forbid")

    code: str
    loc: list[str | int]


class ProblemDetails(BaseModel):
    """RFC 9457 members plus CareerDesk's stable public extensions."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int = Field(ge=400, le=599)
    detail: str | None = None
    request_id: str
    code: str
    params: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    errors: list[ProblemValidationItem] | None = None


def ensure_request_id(scope: Scope) -> str:
    """Get a server-owned request ID without reflecting a spoofable inbound header."""
    state = scope.get("state")
    if not isinstance(state, dict):
        state = {}
        scope["state"] = state
    existing = state.get("request_id")
    if isinstance(existing, str) and existing:
        return existing
    request_id = str(uuid4())
    state["request_id"] = request_id
    return request_id


def _status_title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP Error"


def problem_response(
    scope: Scope,
    *,
    status_code: int,
    detail: str | None = None,
    type_uri: str = "about:blank",
    title: str | None = None,
    headers: Mapping[str, str] | None = None,
    code: str | None = None,
    params: Mapping[str, str | int | float | bool | None] | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """Build RFC 9457 JSON whose extensions cannot override stable public members."""
    request_id = ensure_request_id(scope)
    content: dict[str, Any] = {
        "type": type_uri,
        "title": title or _status_title(status_code),
        "status": status_code,
        "code": code or f"http_{status_code}",
        "params": dict(params or {}),
    }
    if detail is not None:
        content["detail"] = detail
    if extensions:
        collisions = _RESERVED_MEMBERS.intersection(extensions)
        if collisions:
            names = ", ".join(sorted(collisions))
            raise ValueError(f"Problem Details extensions override reserved members: {names}")
        content.update(extensions)
    content["request_id"] = request_id

    response_headers = dict(headers or {})
    response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers=response_headers,
        media_type="application/problem+json",
    )


async def send_problem(
    scope: Scope,
    receive: Receive,
    send: Send,
    **kwargs: Any,
) -> None:
    """Return the same contract from body, origin, and other pure ASGI boundaries."""
    response = problem_response(scope, **kwargs)
    await response(scope, receive, send)


def _normalize_http_detail(detail: Any, status_code: int) -> str | None:
    if isinstance(detail, str):
        return detail
    if detail is None:
        return None
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    # Do not echo unknown structures that may carry third-party internal fields.
    return _status_title(status_code)


def _public_validation_errors(error: RequestValidationError) -> list[dict[str, Any]]:
    """Expose only location and stable type; raw input/context may contain private data."""
    public_errors = []
    for item in error.errors():
        public_errors.append({
            "code": str(item.get("type") or "validation_error"),
            "loc": jsonable_encoder(item.get("loc", [])),
        })
    return public_errors


async def _http_exception_handler(
    request: Request,
    error: StarletteHTTPException,
) -> JSONResponse:
    return problem_response(
        request.scope,
        status_code=error.status_code,
        detail=_normalize_http_detail(error.detail, error.status_code),
        headers=error.headers,
    )


async def _validation_exception_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    return problem_response(
        request.scope,
        status_code=422,
        type_uri=VALIDATION_PROBLEM_TYPE,
        title="Request validation failed",
        detail="请求参数校验失败。",
        code="request_validation",
        extensions={"errors": _public_validation_errors(error)},
    )


async def _unhandled_exception_handler(request: Request, error: Exception) -> JSONResponse:
    request_id = ensure_request_id(request.scope)
    logger.error(
        "unhandled HTTP request failure request_id=%s error_type=%s",
        request_id,
        type(error).__name__,
    )
    return problem_response(
        request.scope,
        status_code=500,
        detail="服务器处理请求时发生错误。",
        code="internal_error",
    )


def install_problem_details_handlers(app: FastAPI) -> None:
    """Map framework, validation, and unknown failures to Problem Details."""
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)


class RequestIdMiddleware:
    """Attach one server-owned request ID to successful and failed responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_id = ensure_request_id(scope)

        async def send_with_request_id(message: Message) -> None:
            if message.get("type") != "http.response.start":
                await send(message)
                return
            outgoing = dict(message)
            outgoing["headers"] = list(message.get("headers", []))
            headers = MutableHeaders(scope=outgoing)
            headers[REQUEST_ID_HEADER] = request_id
            await send(outgoing)

        await self.app(scope, receive, send_with_request_id)


__all__ = [
    "ProblemDetails",
    "REQUEST_ID_HEADER",
    "RequestIdMiddleware",
    "install_problem_details_handlers",
    "problem_response",
    "send_problem",
]
