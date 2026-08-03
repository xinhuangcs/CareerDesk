"""Resume API for text/file creation, versions, replacement, and safe archival."""

import asyncio
import logging
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, model_validator

from ...auth import current_user_id
from ...core.config import get_settings
from ...platform.ai.client import build_llm, close_llm_client
from ...platform.storage.documents import DOCUMENT_SUFFIXES, extract_document_text
from ...platform.storage.uploads import (MAX_CHAT_OR_RESUME_BYTES, MAX_RESUME_STORAGE_BYTES,
                                         UploadTooLarge, save_upload, user_upload_root)
from . import jobs, repository as resumes
from .contracts import (
    ResumeDeleteResponse,
    ResumeJobDismissResponse,
    ResumeJobsResponse,
    ResumeMutationResponse,
    ResumeTextResponse,
    ResumesResponse,
)
from .policy import MAX_RESUME_TEXT_CHARS, validate_resume_text
from .service import DUPLICATE_NAME_MESSAGE, ResumeService

router = APIRouter(prefix="/api/resumes")
logger = logging.getLogger(__name__)
DOCUMENT_EXTRACTION_DEADLINE_SECONDS = 60


def _save_upload(file: UploadFile, user_id: str) -> Path:
    """Save a bounded resume file under the current user without partial failures."""
    root = user_upload_root(Path(get_settings().db_path).parent, "resumes", user_id)
    return save_upload(
        file.file,
        file.filename,
        root,
        MAX_CHAT_OR_RESUME_BYTES,
        max_total_bytes=MAX_RESUME_STORAGE_BYTES,
    )


async def _save_or_413(file: UploadFile, user_id: str) -> Path:
    """Move disk I/O off-loop and map size or user-quota overflow to HTTP 413."""
    try:
        return await asyncio.to_thread(_save_upload, file, user_id)
    except UploadTooLarge as error:
        raise HTTPException(status_code=413, detail=str(error)) from error


class RegisterRequest(BaseModel):
    """Resume creation payload with text, version name, and binding mode."""

    name: str = Field(min_length=1, max_length=200)
    content_text: str = Field(min_length=1, max_length=MAX_RESUME_TEXT_CHARS)
    binding: Literal["family", "application"] = "family"
    application_id: int | None = Field(default=None, gt=0)
    family: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_binding(self):
        """Reject inconsistent bindings before any model call."""
        if self.binding == "application" and self.application_id is None:
            raise ValueError("binding=application 时必须传 application_id")
        if self.binding == "family" and self.application_id is not None:
            raise ValueError("binding=family 时不能传 application_id")
        return self


class UpdateResumeTextRequest(BaseModel):
    """Manual correction of the model-normalized text shown in Library."""

    content_text: str = Field(min_length=1, max_length=MAX_RESUME_TEXT_CHARS)
    expected_content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


@router.post(
    "",
    response_model=ResumeMutationResponse,
    response_model_exclude_unset=True,
)
async def register_resume(req: RegisterRequest, user_id: str = Depends(current_user_id)) -> dict:
    """Create a resume version with one synchronous model parsing call."""
    settings = get_settings()
    # Reject duplicate creation before constructing an LLM client or changing data.
    if resumes.resume_name_exists(settings.db_path, user_id, req.name):
        return {"status": "error", "message": DUPLICATE_NAME_MESSAGE}
    llm = build_llm(
        settings.llm_model,
        strict_offline=settings.strict_offline,
        context_window=getattr(settings, "llm_context_window", None),
        max_output_tokens=getattr(settings, "llm_max_output_tokens", None),
    ) if settings.llm_model is not None else None
    try:
        service = ResumeService(settings.db_path, llm)
        return await service.register(
            user_id, req.name, req.content_text,
            binding=req.binding, application_id=req.application_id,
            family=req.family, replace_existing=False,
        )
    finally:
        await close_llm_client(llm)


@router.get("", response_model=ResumesResponse)
def get_resumes(user_id: str = Depends(current_user_id)) -> dict:
    """List lightweight resume versions without copying full text or segments."""
    return {"items": resumes.list_resume_summaries(get_settings().db_path, user_id)}


@router.get("/{resume_id}/text", response_model=ResumeTextResponse)
def get_resume_text(resume_id: int, user_id: str = Depends(current_user_id)) -> dict:
    """Return corrected text only after an explicit per-resume request."""
    item = resumes.get_active_resume_text(get_settings().db_path, user_id, resume_id)
    if item is None:
        raise HTTPException(status_code=404, detail="简历不存在或已归档")
    return item


@router.put("/{resume_id}/text", response_model=ResumeTextResponse)
def update_resume_text(
    resume_id: int,
    req: UpdateResumeTextRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Save a user's manual correction without replacing the uploaded source file."""
    try:
        status, item = resumes.update_active_resume_text(
            get_settings().db_path,
            user_id,
            resume_id,
            req.content_text,
            expected_content_hash=req.expected_content_hash,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if status == "not_found":
        raise HTTPException(status_code=404, detail="简历不存在或已归档")
    if status == "stale":
        raise HTTPException(status_code=409, detail="简历文字已在其他位置更新，请重新打开后再编辑")
    assert item is not None
    return item


@router.get("/jobs", response_model=ResumeJobsResponse)
def get_resume_jobs(user_id: str = Depends(current_user_id)) -> dict:
    """Return server-owned file processing state across page changes and reloads."""
    return {"items": jobs.list_jobs(get_settings().db_path, user_id)}


@router.delete("/jobs/{job_id}", response_model=ResumeJobDismissResponse)
def dismiss_resume_job(job_id: UUID, user_id: str = Depends(current_user_id)) -> dict:
    """Dismiss a completed or failed task card; active work remains visible."""
    try:
        dismissed = jobs.dismiss_job(
            get_settings().db_path,
            user_id,
            str(job_id),
        )
    except jobs.ResumeJobConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "ok", "dismissed": dismissed}


async def _run_file_job(
    *,
    db_path: str,
    user_id: str,
    job_id: str,
    destination: Path,
    llm,
    name: str,
    binding: str,
    application_id: int | None,
    family: str | None,
    replace_existing: bool,
    expected_update: resumes.ResumeUpdateSnapshot | None,
) -> None:
    keep = False
    try:
        jobs.update_job(db_path, user_id, job_id, stage="extracting")
        try:
            content_text = await asyncio.wait_for(
                asyncio.to_thread(extract_document_text, str(destination)),
                timeout=DOCUMENT_EXTRACTION_DEADLINE_SECONDS,
            )
            validate_resume_text(content_text)
        except TimeoutError:
            jobs.update_job(
                db_path, user_id, job_id, state="failed", stage="failed",
                message="文档读取超过 60 秒，已停止等待。若是扫描版 PDF，请先 OCR 后重试。",
            )
            return
        except ValueError as error:
            jobs.update_job(
                db_path, user_id, job_id, state="failed", stage="failed",
                message=str(error),
            )
            return
        jobs.update_job(db_path, user_id, job_id, stage="parsing")
        result = await ResumeService(db_path, llm).register(
            user_id,
            name,
            content_text,
            binding=binding,
            application_id=application_id,
            family=family,
            file_path=str(destination),
            replace_existing=replace_existing,
            expected_update=expected_update,
        )
        if result.get("status") != "ok":
            jobs.update_job(
                db_path, user_id, job_id, state="failed", stage="failed",
                message=result.get("message") or "简历解析失败，请重试。",
            )
            return
        keep = True
        completion_message = "简历已解析并保存。"
        if result.get("cleanup_warning"):
            completion_message += f" {result['cleanup_warning']}"
        jobs.update_job(
            db_path, user_id, job_id, state="completed", stage="completed",
            message=completion_message, resume_id=result["resume_id"],
        )
    except Exception:  # noqa: BLE001 -- task state must reach a safe terminal result
        logger.exception("resume file job failed")
        jobs.update_job(
            db_path, user_id, job_id, state="failed", stage="failed",
            message="简历处理失败，请确认文件可读取后重试。",
        )
    finally:
        if not keep:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to remove rejected resume upload")
        await close_llm_client(llm)


@router.post(
    "/upload",
    response_model=ResumeMutationResponse,
    response_model_exclude_unset=True,
)
async def upload_resume(background: BackgroundTasks,
                        file: UploadFile = File(...),
                        name: str | None = Form(None, max_length=200),
                        binding: Literal["family", "application"] = Form("family"),
                        application_id: int | None = Form(None, gt=0),
                        family: str | None = Form(None, max_length=100),
                        user_id: str = Depends(current_user_id)) -> dict:
    """Extract and parse a PDF/DOCX/MD/TXT through the same creation pipeline."""
    settings = get_settings()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        return {"status": "error", "message": f"不支持的简历格式 {suffix}（支持 pdf/docx/md/txt）"}
    if binding == "application" and application_id is None:
        raise HTTPException(status_code=422, detail="binding=application 时必须传 application_id")
    if binding == "family" and application_id is not None:
        raise HTTPException(status_code=422, detail="binding=family 时不能传 application_id")
    resolved_name = name or Path(file.filename or "简历").stem
    if len(resolved_name) > 200:
        raise HTTPException(status_code=422, detail="简历名称不能超过 200 个字符")
    # Check before saving so duplicate names neither overwrite data nor orphan an upload.
    if resumes.resume_name_exists(settings.db_path, user_id, resolved_name):
        return {"status": "error", "message": DUPLICATE_NAME_MESSAGE}
    # LLM construction performs local configuration checks only and must precede upload.
    # Offline, credential, or provider failures must not leave an unregistered file.
    llm = build_llm(
        settings.llm_model,
        strict_offline=settings.strict_offline,
        context_window=getattr(settings, "llm_context_window", None),
        max_output_tokens=getattr(settings, "llm_max_output_tokens", None),
    ) if settings.llm_model is not None else None
    destination: Path | None = None
    try:
        destination = await _save_or_413(file, user_id)
        try:
            job = jobs.start_job(
                settings.db_path,
                user_id,
                operation="create",
                name=resolved_name,
                file_path=str(destination),
            )
        except (ValueError, jobs.ResumeJobConflict) as error:
            destination.unlink(missing_ok=True)
            await close_llm_client(llm)
            return {"status": "error", "message": str(error)}
        background.add_task(
            _run_file_job,
            db_path=settings.db_path,
            user_id=user_id,
            job_id=job["job_id"],
            destination=destination,
            llm=llm,
            name=resolved_name,
            binding=binding,
            application_id=application_id,
            family=family,
            replace_existing=False,
            expected_update=None,
        )
        return {"status": "processing", "job_id": job["job_id"]}
    except BaseException:
        if destination is not None:
            destination.unlink(missing_ok=True)
        await close_llm_client(llm)
        raise


@router.put(
    "/{resume_id}",
    response_model=ResumeMutationResponse,
    response_model_exclude_unset=True,
)
async def update_resume(resume_id: int,
                        background: BackgroundTasks,
                        file: UploadFile = File(...),
                        user_id: str = Depends(current_user_id)) -> dict:
    """Replace resume content while retaining the existing name and binding.

    Freeze the old row and use an in-transaction CAS by resume ID. Bindings come from
    storage, so late results cannot overwrite a newer version or revive an archive.
    """
    settings = get_settings()
    existing = resumes.get_resume_update_snapshot(settings.db_path, user_id, resume_id)
    if existing is None:
        return {"status": "error", "message": f"找不到简历 #{resume_id}"}
    if existing.archived:
        return {"status": "error", "message": "该简历已归档，不能再更新；请新建一个版本"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        return {"status": "error", "message": f"不支持的简历格式 {suffix}（支持 pdf/docx/md/txt）"}
    llm = build_llm(
        settings.llm_model,
        strict_offline=settings.strict_offline,
        context_window=getattr(settings, "llm_context_window", None),
        max_output_tokens=getattr(settings, "llm_max_output_tokens", None),
    ) if settings.llm_model is not None else None
    destination: Path | None = None
    try:
        destination = await _save_or_413(file, user_id)
        try:
            job = jobs.start_job(
                settings.db_path,
                user_id,
                operation="update",
                name=existing.name,
                file_path=str(destination),
                target_resume_id=resume_id,
            )
        except (ValueError, jobs.ResumeJobConflict) as error:
            destination.unlink(missing_ok=True)
            await close_llm_client(llm)
            return {"status": "error", "message": str(error)}
        background.add_task(
            _run_file_job,
            db_path=settings.db_path,
            user_id=user_id,
            job_id=job["job_id"],
            destination=destination,
            llm=llm,
            name=existing.name,
            binding=existing.binding,
            application_id=existing.application_id,
            family=existing.family,
            replace_existing=True,
            expected_update=existing,
        )
        return {"status": "processing", "job_id": job["job_id"]}
    except BaseException:
        if destination is not None:
            destination.unlink(missing_ok=True)
        await close_llm_client(llm)
        raise


@router.delete(
    "/{resume_id}",
    response_model=ResumeDeleteResponse,
    response_model_exclude_unset=True,
)
def remove_resume(resume_id: int, user_id: str = Depends(current_user_id)) -> dict:
    """Archive a resume version because bindings and historical artifacts reference it."""
    settings = get_settings()
    removed, file_path = resumes.archive_resume_with_file(settings.db_path, user_id, resume_id)
    if not removed:
        return {"status": "error", "message": f"找不到简历 #{resume_id}"}
    warning = None
    if file_path:
        root = user_upload_root(Path(settings.db_path).parent, "resumes", user_id)
        candidate = Path(file_path).expanduser()
        try:
            resolved = candidate.resolve()
            if not candidate.is_symlink() and resolved.parent == root:
                candidate.unlink(missing_ok=True)
        except OSError as error:
            warning = f"版本已归档，但原文件清理失败：{error}"
    return {"status": "ok", **({"cleanup_warning": warning} if warning else {})}
