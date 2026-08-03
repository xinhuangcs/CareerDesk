"""Enforce body limits before multipart/JSON parsing can exhaust disk or memory."""

import re
from tempfile import SpooledTemporaryFile

from .problem_details import send_problem


MIB = 1024 * 1024
DEFAULT_JSON_BODY_BYTES = 2 * MIB
BODY_SPOOL_MEMORY_BYTES = MIB
BODY_REPLAY_CHUNK_BYTES = 64 * 1024
_RESUME_UPDATE = re.compile(r"^/api/resumes/\d+$")
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def request_limit(method: str, path: str, content_type: str = "") -> int | None:
    """Return the raw HTTP body limit for an endpoint, including multipart overhead.

    Upload endpoints keep their larger dedicated budgets. Other ``/api`` writes use
    the default limit, including requests without Content-Type, while future multipart
    endpoints are not accidentally constrained to the smaller JSON allowance.
    """
    method = method.upper()
    if path in (
        "/api/uploads", "/api/resumes/upload", "/api/timeline/intake-operations/file",
    ) or (
            method == "PUT" and _RESUME_UPDATE.fullmatch(path)):
        return 11 * MIB
    if method == "POST" and path == "/api/chat":
        # Allow 160k attachment characters plus a 50k message at four bytes per code point.
        return MIB
    media_type = content_type.partition(";")[0].strip().lower()
    if (method in _WRITE_METHODS and (path == "/api" or path.startswith("/api/"))
            and media_type != "multipart/form-data"):
        return DEFAULT_JSON_BODY_BYTES
    return None


class _BodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """ASGI middleware that checks Content-Length or counts a chunked body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_type = headers.get(b"content-type", b"").decode("latin-1")
        limit = request_limit(scope.get("method", ""), scope.get("path", ""), content_type)
        if limit is None:
            await self.app(scope, receive, send)
            return

        raw_length = headers.get(b"content-length")
        declared_length: int | None = None
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
                if declared_length < 0:
                    declared_length = None
                elif declared_length > limit:
                    await self._reject(scope, receive, send, limit)
                    return
            except ValueError:
                declared_length = None

        # Without a trusted Content-Length, spool chunks within a bound: small requests
        # stay in memory and larger uploads spill to disk. This prevents chunked bypasses
        # without duplicating large bodies into a Python bytes list.
        if declared_length is None:
            spool = SpooledTemporaryFile(max_size=BODY_SPOOL_MEMORY_BYTES)
            received = 0
            try:
                while True:
                    message = await receive()
                    if message.get("type") == "http.disconnect":
                        return
                    if message.get("type") != "http.request":
                        continue
                    chunk = message.get("body", b"")
                    received += len(chunk)
                    if received > limit:
                        await self._reject(scope, receive, send, limit)
                        return
                    spool.write(chunk)
                    if not message.get("more_body", False):
                        break
                spool.seek(0)
                replay_finished = False

                async def replay_receive():
                    nonlocal replay_finished
                    if replay_finished:
                        # Replay the request body once, then wait on the real ASGI receive
                        # so StreamingResponse can observe http.disconnect. Repeated empty
                        # http.request events would spin Starlette's disconnect listener.
                        return await receive()
                    chunk = spool.read(BODY_REPLAY_CHUNK_BYTES)
                    more_body = spool.tell() < received
                    if not more_body:
                        replay_finished = True
                    return {"type": "http.request", "body": chunk,
                            "more_body": more_body}

                await self.app(scope, replay_receive, send)
            finally:
                spool.close()
            return

        received = 0
        too_large = False
        response_started = False

        async def limited_receive():
            nonlocal received, too_large
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message):
            nonlocal response_started
            # FastAPI normalizes receive() exceptions to 400. Once the overflow flag is
            # set, suppress that response and emit the accurate 413 from this middleware.
            if not too_large:
                if message.get("type") == "http.response.start":
                    response_started = True
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass
        if too_large and not response_started:
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope, receive, send, limit: int) -> None:
        await send_problem(
            scope,
            receive,
            send,
            status_code=413,
            detail=(
                f"请求体过大（该端点上限 {limit // MIB if limit >= MIB else limit // 1024} "
                f"{'MB' if limit >= MIB else 'KB'}）"
            ),
        )
