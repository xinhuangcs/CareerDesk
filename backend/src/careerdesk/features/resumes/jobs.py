"""Resume file processing jobs stored in the existing private metadata table."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from ...platform.database import now_iso, read_connection, transaction

_PREFIX = "resume_job:v1:"
_MAX_JOBS_PER_USER = 20
_MAX_ACTIVE_JOBS = 3
_STALE_SECONDS = 10 * 60
_PROCESSING_STAGES = {"queued", "extracting", "parsing", "saving"}
_STATES = {"processing", "completed", "failed"}


class ResumeJobConflict(RuntimeError):
    """A resume job could not be created or updated safely."""


def _state_matches_stage(state: str, stage: str) -> bool:
    return (
        (state == "processing" and stage in _PROCESSING_STAGES)
        or (state == "completed" and stage == "completed")
        or (state == "failed" and stage == "failed")
    )


def _scope(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    return hashlib.sha256(f"careerdesk:resume-job:v1\0{user_id}".encode()).hexdigest()


def _key(user_id: str, job_id: str) -> str:
    return f"{_PREFIX}{_scope(user_id)}:{job_id}"


def _canonical_uuid(value: str) -> str:
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError) as error:
        raise ValueError("job_id 必须是 UUID") from error
    if value != canonical:
        raise ValueError("job_id 必须是规范 UUID")
    return canonical


def _decode(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ResumeJobConflict("简历任务状态已损坏") from error
    required = {
        "job_id", "operation", "target_resume_id", "name", "file_path",
        "state", "stage", "message", "resume_id", "created_time", "updated_time",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("operation") not in {"create", "update"}
        or value.get("state") not in _STATES
        or value.get("stage") not in _PROCESSING_STAGES | {"completed", "failed"}
        or not _state_matches_stage(value.get("state"), value.get("stage"))
        or not isinstance(value.get("name"), str)
        or not value["name"].strip()
        or len(value["name"]) > 200
        or not isinstance(value.get("file_path"), str)
        or not isinstance(value.get("created_time"), str)
        or not isinstance(value.get("updated_time"), str)
        or (value.get("message") is not None and not isinstance(value["message"], str))
        or (value.get("resume_id") is not None and type(value["resume_id"]) is not int)
        or (value.get("target_resume_id") is not None
            and type(value["target_resume_id"]) is not int)
        or (value["operation"] == "create" and value["target_resume_id"] is not None)
        or (value["operation"] == "update"
            and (type(value["target_resume_id"]) is not int
                 or value["target_resume_id"] < 1))
        or (value["state"] == "completed"
            and (type(value["resume_id"]) is not int or value["resume_id"] < 1))
        or (value["state"] != "completed" and value["resume_id"] is not None)
    ):
        raise ResumeJobConflict("简历任务状态已损坏")
    _canonical_uuid(value["job_id"])
    return value


def _rows(conn, user_id: str) -> list[tuple[str, dict]]:
    prefix = f"{_PREFIX}{_scope(user_id)}:"
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
        (f"{prefix}%",),
    ).fetchall()
    if len(rows) > _MAX_JOBS_PER_USER:
        raise ResumeJobConflict("简历任务数量超过安全上限")
    return [(key, _decode(raw)) for key, raw in rows]


def start_job(
    db_path: str,
    user_id: str,
    *,
    operation: str,
    name: str,
    file_path: str,
    target_resume_id: int | None = None,
) -> dict:
    if operation not in {"create", "update"}:
        raise ValueError("operation 无效")
    if operation == "create" and target_resume_id is not None:
        raise ValueError("新建任务不能绑定 target_resume_id")
    if operation == "update" and (
        type(target_resume_id) is not int or target_resume_id < 1
    ):
        raise ValueError("更新任务必须绑定有效的 target_resume_id")
    if not name.strip() or len(name) > 200 or not file_path:
        raise ValueError("简历任务参数无效")
    job_id = str(uuid4())
    timestamp = now_iso()
    job = {
        "job_id": job_id,
        "operation": operation,
        "target_resume_id": target_resume_id,
        "name": name.strip(),
        "file_path": file_path,
        "state": "processing",
        "stage": "queued",
        "message": None,
        "resume_id": None,
        "created_time": timestamp,
        "updated_time": timestamp,
    }
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _rows(conn, user_id)
        active = [item for _, item in existing if item["state"] == "processing"]
        if len(active) >= _MAX_ACTIVE_JOBS:
            raise ResumeJobConflict("已有多份简历正在处理，请等待其中一份完成后再试")
        if any(
            item["name"] == job["name"]
            or (target_resume_id is not None and item["target_resume_id"] == target_resume_id)
            for item in active
        ):
            raise ResumeJobConflict("这份简历已有处理任务正在运行")
        terminal = sorted(
            ((key, item) for key, item in existing if item["state"] != "processing"),
            key=lambda pair: pair[1]["updated_time"],
        )
        remove_count = max(0, len(existing) + 1 - _MAX_JOBS_PER_USER)
        for key, _ in terminal[:remove_count]:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            (_key(user_id, job_id), json.dumps(job, ensure_ascii=False, sort_keys=True)),
        )
    return _public(job)


def update_job(
    db_path: str,
    user_id: str,
    job_id: str,
    *,
    state: str = "processing",
    stage: str,
    message: str | None = None,
    resume_id: int | None = None,
) -> bool:
    canonical_id = _canonical_uuid(job_id)
    if (
        state not in _STATES
        or stage not in _PROCESSING_STAGES | {"completed", "failed"}
        or not _state_matches_stage(state, stage)
        or (state == "completed" and (type(resume_id) is not int or resume_id < 1))
        or (state != "completed" and resume_id is not None)
    ):
        raise ValueError("简历任务状态无效")
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        key = _key(user_id, canonical_id)
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return False
        current = _decode(row[0])
        if current["state"] != "processing":
            return current["state"] == state and current["resume_id"] == resume_id
        updated = {
            **current,
            "state": state,
            "stage": stage,
            "message": message,
            "resume_id": resume_id,
            "updated_time": now_iso(),
        }
        changed = conn.execute(
            "UPDATE meta SET value = ? WHERE key = ? AND value = ?",
            (json.dumps(updated, ensure_ascii=False, sort_keys=True), key, row[0]),
        ).rowcount
        return changed == 1


def dismiss_job(db_path: str, user_id: str, job_id: str) -> bool:
    """Forget one terminal UI task card without exposing another user's jobs.

    Missing jobs are an idempotent no-op.  A processing job still owns work and
    must remain visible, so it cannot be dismissed until it reaches a terminal
    state.  The exact raw value guard keeps a future state-shape change from
    turning this acknowledgement into an unqualified delete.
    """
    canonical_id = _canonical_uuid(job_id)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        key = _key(user_id, canonical_id)
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return False
        current = _decode(row[0])
        if current["state"] == "processing":
            raise ResumeJobConflict("简历仍在处理中，完成或失败后才能关闭这条任务提示")
        removed = conn.execute(
            "DELETE FROM meta WHERE key = ? AND value = ?",
            (key, row[0]),
        ).rowcount
        if removed != 1:
            raise ResumeJobConflict("简历任务状态已变化，请刷新后重试")
        return True


def _public(job: dict) -> dict:
    return {key: value for key, value in job.items() if key != "file_path"}


def list_jobs(db_path: str, user_id: str) -> list[dict]:
    with read_connection(db_path) as conn:
        rows = _rows(conn, user_id)
    now = datetime.now(timezone.utc)
    result: list[dict] = []
    for _, job in rows:
        if job["state"] == "processing":
            try:
                updated = datetime.fromisoformat(job["updated_time"])
                age = (now - updated.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                age = _STALE_SECONDS + 1
            if age > _STALE_SECONDS:
                update_job(
                    db_path,
                    user_id,
                    job["job_id"],
                    state="failed",
                    stage="failed",
                    message="任务已中断（应用可能曾退出），请重新上传。",
                )
                job = {**job, "state": "failed", "stage": "failed",
                       "message": "任务已中断（应用可能曾退出），请重新上传。"}
        result.append(_public(job))
    result.sort(key=lambda item: item["created_time"], reverse=True)
    return result
