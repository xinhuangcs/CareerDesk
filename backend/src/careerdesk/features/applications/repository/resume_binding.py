"""Application-owned resume binding mutation.

The selected resume is part of the application profile, so the mutation and
its cache invalidation belong to Applications rather than to the adaptation
orchestration layer.
"""

import json

from ....platform.database import INTERACTIVE_BUSY_TIMEOUT_MS, loads_json, now_iso, transaction
from .shared import TimelineMutationConflict


_RESUME_DEPENDENT_PREP_KEYS = frozenset({
    "resume_adaptation",
    "resume_adaptation_summary",
})


def bind_application_resume(
    db_path: str,
    user_id: str,
    application_id: int,
    resume_id: int | None,
    *,
    expected_edit_revision: int,
) -> dict | None:
    """Bind or unbind an owned active resume using the application edit CAS.

    Only resume-derived artifacts are invalidated.  In particular, a valid
    research snapshot and its attempt state survive this profile edit.
    """
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        application = conn.execute(
            "SELECT revision, resume_id, prep_json FROM applications "
            "WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if application is None:
            return None
        revision, current_resume_id, prep_json = application
        if revision != expected_edit_revision:
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")

        resume = None
        if resume_id is not None:
            resume = conn.execute(
                "SELECT id, name, updated_time FROM resumes "
                "WHERE user_id = ? AND id = ? AND archived = 0",
                (user_id, resume_id),
            ).fetchone()
            if resume is None:
                raise ValueError("所选简历不存在、已归档或不属于当前用户")

        # A replay with the current revision and the same selection is a true
        # no-op; it should not invalidate paid artifacts or manufacture an edit.
        if current_resume_id == resume_id:
            return {
                "resume_id": resume_id,
                "edit_revision": revision,
                "bound_resume": (
                    {
                        "id": resume[0],
                        "name": resume[1],
                        "updated_time": resume[2],
                        "extraction_receipt": None,
                    }
                    if resume is not None
                    else None
                ),
            }

        loaded = loads_json(prep_json, {})
        prep = loaded if isinstance(loaded, dict) else {}
        for key in _RESUME_DEPENDENT_PREP_KEYS:
            prep.pop(key, None)
        changed_time = now_iso()
        cursor = conn.execute(
            "UPDATE applications SET resume_id = ?, prep_json = ?, "
            "revision = revision + 1, updated_time = ? "
            "WHERE user_id = ? AND id = ? AND revision = ?",
            (
                resume_id,
                json.dumps(prep, ensure_ascii=False) if prep_json is not None else None,
                changed_time,
                user_id,
                application_id,
                expected_edit_revision,
            ),
        )
        if cursor.rowcount != 1:  # pragma: no cover - protected by BEGIN IMMEDIATE
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
        return {
            "resume_id": resume_id,
            "edit_revision": revision + 1,
            "bound_resume": (
                {
                    "id": resume[0],
                    "name": resume[1],
                    "updated_time": resume[2],
                    "extraction_receipt": None,
                }
                if resume is not None
                else None
            ),
        }


__all__ = ["bind_application_resume"]
