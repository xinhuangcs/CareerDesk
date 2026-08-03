"""Sole Assistant HTTP/SSE adapter for chat, recovery, and temporary attachments."""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.sse import EventSourceResponse

from ...auth import current_user_id
from ...platform.http.problem_details import problem_response
from . import service
from .contracts import (
    ChatRecoveryScopeResponse,
    ChatRequest,
    ChatTurnCancelRequest,
    ChatTurnStatusResponse,
    ChatUploadDeleteResponse,
    ChatUploadResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _encode_sse(event: str, data: dict) -> str:
    """Encode returned StreamingResponse events explicitly."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


class _ClaimedEventSourceResponse(EventSourceResponse):
    """Response owner guard that closes running even before first body iteration."""

    def __init__(self, content, prepared: service.PreparedChat):
        self._prepared = prepared
        super().__init__(content)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if callable(close):
                    await close()
            except Exception:  # noqa: BLE001 -- transport failure must still release ledger owner
                logger.exception("failed to close assistant response iterator")
            finally:
                service.abandon_prepared_chat(self._prepared)


async def _encode_prepared_events(prepared: service.PreparedChat):
    """Encode neutral events as SSE and close the agent stream on early transport exit."""
    terminal_sent = False
    trace_id = uuid.uuid4().hex
    stream = service.stream_prepared_chat(prepared)
    try:
        try:
            async for event in stream:
                if terminal_sent:
                    logger.error(
                        "assistant emitted an event after terminal",
                        extra={"trace_id": trace_id},
                    )
                    return
                yield _encode_sse(event.event, event.data)
                if event.event in {"done", "error"}:
                    terminal_sent = True
                    return
        except Exception:  # noqa: BLE001 -- SSE headers may already be sent
            logger.exception("assistant stream encoding failed", extra={"trace_id": trace_id})
            if terminal_sent:
                return
            error = service.abandon_prepared_chat(prepared, trace_id=trace_id)
            yield _encode_sse("error", error)
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()


def _chat_turn_rejected_response(
    request: Request,
    error: service.ChatTurnRejected,
) -> Response:
    headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
    return problem_response(
        request.scope,
        status_code=409,
        type_uri="urn:careerdesk:problem:chat-turn-rejected",
        title="Chat turn rejected",
        detail=error.data["message"],
        headers=headers,
        code=str(error.data.get("code") or "chat_turn_rejected"),
        extensions={
            key: value
            for key, value in error.data.items()
            if key not in {"message", "code"}
        },
    )


@router.post(
    "/chat",
    response_class=Response,
    responses={
        200: {
            "description": "Assistant event stream",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
    },
)
async def chat(
    req: ChatRequest,
    request: Request,
    user_id: str = Depends(current_user_id),
):
    """Claim before headers and encode exactly one terminal SSE event."""
    attachments = [item.model_dump(mode="json", exclude_none=True) for item in req.attachments]
    try:
        prepared = service.prepare_chat(
            req.message,
            str(req.session_id),
            str(req.client_turn_id),
            user_id,
            attachments=attachments,
            review_supplement_reference=(
                str(req.review_supplement_reference)
                if req.review_supplement_reference is not None
                else None
            ),
            output_locale=req.output_locale,
        )
    except service.ChatTurnRejected as error:
        return _chat_turn_rejected_response(request, error)

    return _ClaimedEventSourceResponse(_encode_prepared_events(prepared), prepared)


@router.get(
    "/chat/turns/{client_turn_id}/status",
    response_model=ChatTurnStatusResponse,
)
def get_chat_turn_status(
    client_turn_id: UUID,
    user_id: str = Depends(current_user_id),
) -> ChatTurnStatusResponse:
    """Read-only check that an agent turn fully exited for reconnect recovery."""
    return service.get_chat_turn_status(user_id, str(client_turn_id))


@router.post(
    "/chat/turns/{client_turn_id}/cancel-if-absent",
    response_model=ChatTurnStatusResponse,
)
def cancel_chat_turn_if_absent(
    client_turn_id: UUID,
    _command: ChatTurnCancelRequest,
    user_id: str = Depends(current_user_id),
) -> ChatTurnStatusResponse:
    """Seal an absent turn UUID permanently without cancelling a started agent."""
    return service.cancel_chat_turn_if_absent(user_id, str(client_turn_id))


@router.post(
    "/chat/turns/{client_turn_id}/cancel",
    response_model=ChatTurnStatusResponse,
)
async def cancel_chat_turn(
    client_turn_id: UUID,
    _command: ChatTurnCancelRequest,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> ChatTurnStatusResponse | Response:
    """Request cooperative cancellation for the current turn."""
    try:
        return service.cancel_chat_turn(user_id, str(client_turn_id))
    except service.ChatTurnRejected as error:
        return _chat_turn_rejected_response(request, error)


@router.get(
    "/chat/recovery-scope",
    response_model=ChatRecoveryScopeResponse,
)
def get_chat_recovery_scope(
    user_id: str = Depends(current_user_id),
) -> ChatRecoveryScopeResponse:
    """Return a stable recovery namespace without exposing the raw user ID."""
    return service.get_chat_recovery_scope(user_id)


async def _save_or_413(file: UploadFile, user_id: str) -> Path:
    try:
        return await asyncio.to_thread(
            service.save_chat_upload, file.file, file.filename, user_id,
        )
    except service.ChatUploadTooLarge as error:
        raise HTTPException(status_code=413, detail=str(error)) from error


@router.post(
    "/uploads",
    response_model=ChatUploadResponse,
    response_model_exclude_unset=True,
)
async def upload_chat_attachment(file: UploadFile = File(...),
                                 user_id: str = Depends(current_user_id)) -> dict:
    """Keep images until send succeeds; delete documents after bounded extraction."""
    kind = service.classify_attachment(file.filename)
    if kind is None:
        suffix = Path(file.filename or "").suffix.lower()
        return {
            "status": "error",
            "message": (
                f"不支持的附件格式 {suffix or '（无后缀）'}"
                "（支持 pdf/docx/md/txt/xlsx/xls/csv/tsv 与常见图片）"
            ),
        }
    destination = await _save_or_413(file, user_id)
    if kind == "image":
        return {
            "status": "ok", "kind": "image", "filename": file.filename,
            "stored": destination.name,
        }
    try:
        return await asyncio.to_thread(
            service.extract_chat_document, destination, file.filename,
        )
    except ValueError as error:
        return {"status": "error", "message": str(error)}


@router.delete("/uploads/{stored}", response_model=ChatUploadDeleteResponse)
async def delete_chat_attachment(stored: str,
                                 user_id: str = Depends(current_user_id)) -> dict:
    try:
        await asyncio.to_thread(service.delete_chat_upload, stored, user_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"status": "ok"}
