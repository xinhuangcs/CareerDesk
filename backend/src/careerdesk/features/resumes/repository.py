"""Resume persistence for family/role versions, parsed lines, and archiving."""

import json
from dataclasses import dataclass, field

from ...platform.database import loads_json, now_iso, read_connection, transaction
from .policy import (
    canonicalize_resume_text,
    resume_content_hash,
    segment_resume_text,
    validate_resume_text,
)


# These preparation artifacts read resume text directly. Retaining them after an
# update would present conclusions from old text as current.
_RESUME_DEPENDENT_PREP_KEYS = frozenset({
    "resume_adaptation", "resume_adaptation_summary",
})

_RESUME_UPDATE_COLUMNS = (
    "id, name, family, binding, application_id, content_text, lines_json, "
    "file_path, archived, created_time, updated_time"
)


@dataclass(frozen=True)
class ResumeUpdateSnapshot:
    """Complete old-row identity required by an explicit content replacement."""

    resume_id: int
    name: str
    family: str | None
    binding: str
    application_id: int | None
    content_text: str = field(repr=False)
    raw_lines_json: str | None = field(repr=False)
    file_path: str | None = field(repr=False)
    archived: bool
    created_time: str
    updated_time: str


def _resume_update_snapshot_from_row(row) -> ResumeUpdateSnapshot:
    return ResumeUpdateSnapshot(
        resume_id=row[0],
        name=row[1],
        family=row[2],
        binding=row[3],
        application_id=row[4],
        content_text=row[5],
        raw_lines_json=row[6],
        file_path=row[7],
        archived=bool(row[8]),
        created_time=row[9],
        updated_time=row[10],
    )


def _resume_update_snapshot_in_transaction(
    conn,
    user_id: str,
    *,
    resume_id: int | None = None,
    name: str | None = None,
) -> ResumeUpdateSnapshot | None:
    if (resume_id is None) == (name is None):
        raise ValueError("resume snapshot needs exactly one identity")
    if resume_id is not None:
        condition = "id = ?"
        identity = resume_id
    else:
        condition = "name = ?"
        identity = name
    row = conn.execute(
        f"SELECT {_RESUME_UPDATE_COLUMNS} FROM resumes "
        f"WHERE user_id = ? AND {condition}",
        (user_id, identity),
    ).fetchone()
    return _resume_update_snapshot_from_row(row) if row is not None else None


def get_resume_update_snapshot(
    db_path: str,
    user_id: str,
    resume_id: int,
) -> ResumeUpdateSnapshot | None:
    """Freeze a full replacement snapshot by id, including archived state."""
    with read_connection(db_path) as conn:
        return _resume_update_snapshot_in_transaction(
            conn,
            user_id,
            resume_id=resume_id,
        )


def get_resume_update_snapshot_by_name(
    db_path: str,
    user_id: str,
    name: str,
) -> ResumeUpdateSnapshot | None:
    """Freeze a replacement snapshot by version name for safe domain updates."""
    with read_connection(db_path) as conn:
        return _resume_update_snapshot_in_transaction(conn, user_id, name=name)


def resume_update_snapshot_matches(
    db_path: str,
    user_id: str,
    snapshot: ResumeUpdateSnapshot,
) -> bool:
    """Reject stale or archived updates before an expensive model call."""
    with read_connection(db_path) as conn:
        current = _resume_update_snapshot_in_transaction(
            conn,
            user_id,
            resume_id=snapshot.resume_id,
        )
    return current == snapshot and not snapshot.archived


def _validate_binding_target(conn, user_id: str, binding: str,
                             application_id: int | None) -> None:
    """Enforce binding invariants and tenant scope in the resume transaction."""
    if binding not in ("family", "application"):
        raise ValueError("binding 只能是 family 或 application")
    if binding == "family":
        if application_id is not None:
            raise ValueError("binding=family 时不能传 application_id")
        return
    if application_id is None:
        raise ValueError("binding=application 时必须传 application_id")
    owned = conn.execute(
        "SELECT 1 FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone()
    if owned is None:
        raise ValueError(f"找不到岗位 #{application_id}（或无权访问）")


def _invalidate_prep_for_resume(conn, user_id: str, resume_id: int) -> list[int]:
    """Clear resume-derived artifacts without resetting active research/prep work."""
    newest = conn.execute(
        "SELECT id FROM resumes WHERE user_id = ? AND archived = 0 "
        "ORDER BY updated_time DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    is_fallback = newest is not None and newest[0] == resume_id
    rows = conn.execute(
        "SELECT applications.id, applications.prep_json FROM applications "
        "LEFT JOIN resumes AS bound ON bound.id = applications.resume_id "
        "AND bound.user_id = applications.user_id AND bound.archived = 0 "
        "WHERE applications.user_id = ? AND "
        "(applications.resume_id = ? OR (? = 1 AND bound.id IS NULL))",
        (user_id, resume_id, 1 if is_fallback else 0),
    ).fetchall()

    invalidated: list[int] = []
    changed_time = now_iso()
    for application_id, prep_json in rows:
        prep = loads_json(prep_json, {})
        if not isinstance(prep, dict):
            prep = {}
        for key in _RESUME_DEPENDENT_PREP_KEYS:
            prep.pop(key, None)
        cleaned_prep_json = json.dumps(prep, ensure_ascii=False) if prep_json is not None else None
        conn.execute(
            "UPDATE applications SET prep_json = ?, updated_time = ? "
            "WHERE user_id = ? AND id = ?",
            (cleaned_prep_json, changed_time, user_id, application_id),
        )
        invalidated.append(application_id)
    return invalidated


def upsert_resume(db_path: str, user_id: str, name: str, content_text: str, *,
                  family: str | None = None, binding: str = "family",
                  application_id: int | None = None, lines: list[dict] | None = None,
                  file_path: str | None = None,
                  overwrite_existing: bool = True,
                  return_previous_file: bool = False,
                  expected_update: ResumeUpdateSnapshot | None = None,
                  ) -> int | None | tuple[int | None, str | None]:
    """Upsert a user/version resume and bind role-specific versions to applications.

    With ``overwrite_existing=False``, any same-name row, including archived,
    returns none without mutation. Explicit updates use ``expected_update`` and
    replace by id only while the full old row still matches and remains active.
    ``return_previous_file`` returns the replaced path for post-commit cleanup.
    """
    if expected_update is not None and not overwrite_existing:
        raise ValueError("expected_update 只能用于显式替换")
    content_text = canonicalize_resume_text(content_text)
    content_hash = resume_content_hash(content_text)
    segments = segment_resume_text(content_text)
    extraction_receipt = {
        "version": "resume-extraction-v1",
        "canonicalization": "crlf-to-lf",
        "character_count": len(content_text),
        "segment_count": len(segments),
        "content_hash": content_hash,
    }
    with transaction(db_path) as conn:
        # Acquire the write lock before any read to avoid a deferred-transaction
        # upgrade race. Model work is already complete, so this CAS is brief.
        conn.execute("BEGIN IMMEDIATE")
        if expected_update is not None:
            current = _resume_update_snapshot_in_transaction(
                conn,
                user_id,
                resume_id=expected_update.resume_id,
            )
            if (
                current != expected_update
                or expected_update.archived
                or expected_update.name != name
            ):
                return (None, None) if return_previous_file else None
        _validate_binding_target(conn, user_id, binding, application_id)
        previous_file = None
        encoded_lines = json.dumps(lines, ensure_ascii=False) if lines is not None else None
        encoded_segments = json.dumps(segments, ensure_ascii=False, separators=(",", ":"))
        encoded_receipt = json.dumps(extraction_receipt, ensure_ascii=False, separators=(",", ":"))
        annotation_status = "ready" if lines is not None else "pending"
        changed_time = now_iso()

        if expected_update is not None:
            previous_file = expected_update.file_path if file_path is not None else None
            cursor = conn.execute(
                "UPDATE resumes SET family = ?, binding = ?, application_id = ?, "
                "content_text = ?, content_hash = ?, extraction_receipt_json = ?, "
                "segments_json = ?, lines_json = ?, annotation_status = ?, "
                "file_path = COALESCE(?, file_path), updated_time = ? "
                "WHERE user_id = ? AND id = ? AND archived = 0",
                (
                    family,
                    binding,
                    application_id,
                    content_text, content_hash, encoded_receipt, encoded_segments,
                    encoded_lines,
                    annotation_status,
                    file_path,
                    changed_time,
                    user_id,
                    expected_update.resume_id,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - same write-lock invariant
                return (None, None) if return_previous_file else None
            resume_id = expected_update.resume_id
        else:
            existing = _resume_update_snapshot_in_transaction(conn, user_id, name=name)
            if existing is not None and (not overwrite_existing or existing.archived):
                return (None, None) if return_previous_file else None
            if return_previous_file and file_path is not None and existing is not None:
                previous_file = existing.file_path
            if existing is None:
                cursor = conn.execute(
                    "INSERT INTO resumes (user_id, name, family, binding, application_id, "
                    "content_text, content_hash, extraction_receipt_json, segments_json, "
                    "lines_json, annotation_status, file_path, created_time, updated_time) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        name,
                        family,
                        binding,
                        application_id,
                        content_text, content_hash, encoded_receipt, encoded_segments,
                        encoded_lines, annotation_status,
                        file_path,
                        changed_time,
                        changed_time,
                    ),
                )
                resume_id = cursor.lastrowid
            else:
                conn.execute(
                    "UPDATE resumes SET family = ?, binding = ?, application_id = ?, "
                    "content_text = ?, content_hash = ?, extraction_receipt_json = ?, "
                    "segments_json = ?, lines_json = ?, annotation_status = ?, "
                    "file_path = COALESCE(?, file_path), updated_time = ? "
                    "WHERE user_id = ? AND id = ? AND archived = 0",
                    (
                        family,
                        binding,
                        application_id,
                        content_text, content_hash, encoded_receipt, encoded_segments,
                        encoded_lines, annotation_status,
                        file_path,
                        changed_time,
                        user_id,
                        existing.resume_id,
                    ),
                )
                resume_id = existing.resume_id
        if (
            binding == "application"
            and application_id is not None
            and expected_update is None
        ):
            # A role-specific resume becomes that application's selected version.
            # Explicit content replacement does not rewrite a pointer that the user
            # may have changed while waiting for the model.
            conn.execute("UPDATE applications SET resume_id = ?, updated_time = ? WHERE user_id = ? AND id = ?",
                         (resume_id, now_iso(), user_id, application_id))
        # Resume writes and cache invalidation commit together to avoid a permanent
        # new-binding/old-report state after process failure.
        _invalidate_prep_for_resume(conn, user_id, resume_id)
        return (resume_id, previous_file) if return_previous_file else resume_id


def resume_name_exists(db_path: str, user_id: str, name: str) -> bool:
    """Return whether a version name exists, counting archived rows as reserved."""
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM resumes WHERE user_id = ? AND name = ? LIMIT 1", (user_id, name)
        ).fetchone()
    return row is not None


def resume_adaptation_candidates_in_transaction(
    conn,
    user_id: str,
    bound_resume_id: int | None,
) -> dict:
    """Freeze active resume candidates inside an application-owned read snapshot.

    The Applications aggregate owns the outer transaction while Resumes keeps
    its table shape, archive policy and DTO projection private.
    """
    if not getattr(conn, "in_transaction", False):
        raise ValueError("resume candidate read requires an active transaction")
    rows = conn.execute(
        "SELECT id, name, updated_time FROM resumes "
        "WHERE user_id = ? AND archived = 0 "
        "ORDER BY updated_time DESC, id DESC",
        (user_id,),
    ).fetchall()
    candidates = [
        {"id": row[0], "name": row[1], "updated_time": row[2]}
        for row in rows
    ]
    bound_row = None
    if bound_resume_id is not None:
        bound_row = conn.execute(
            "SELECT id, name, content_text, file_path, updated_time, content_hash, "
            "extraction_receipt_json, segments_json, annotation_status "
            "FROM resumes WHERE user_id = ? AND id = ? AND archived = 0",
            (user_id, bound_resume_id),
        ).fetchone()
    bound_resume = None
    if bound_row is not None:
        bound_resume = {
            "id": bound_row[0],
            "name": bound_row[1],
            "content_text": bound_row[2],
            "file_path": bound_row[3],
            "updated_time": bound_row[4],
            "content_hash": bound_row[5],
            "extraction_receipt": loads_json(bound_row[6], {}),
            "segments": loads_json(bound_row[7], []),
            "annotation_status": bound_row[8],
        }
    return {
        "resumes": candidates,
        "bound_resume": bound_resume,
    }


def resume_generation_snapshot_in_transaction(conn, user_id: str, resume_id: int) -> dict | None:
    """Return the immutable semantic input used by interview generation."""
    if not getattr(conn, "in_transaction", False):
        raise ValueError("resume generation snapshot requires an active transaction")
    row = conn.execute(
        "SELECT id, name, content_text, content_hash, extraction_receipt_json, "
        "segments_json, archived FROM resumes WHERE user_id = ? AND id = ?",
        (user_id, resume_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "name": row[1], "content_text": row[2], "content_hash": row[3],
        "extraction_receipt": loads_json(row[4], {}), "segments": loads_json(row[5], []),
        "archived": bool(row[6]),
    }


def get_resume(db_path: str, user_id: str, resume_id: int) -> dict | None:
    """Read a resume and parsed lines by id, returning none when absent."""
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, name, family, binding, application_id, content_text, lines_json, archived, "
            "content_hash, extraction_receipt_json, segments_json, annotation_status "
            "FROM resumes WHERE user_id = ? AND id = ?",
            (user_id, resume_id),
        ).fetchone()
    if row is None:
        return None
    (resume_id, name, family, binding, application_id, content_text, lines_json, archived,
     content_hash, receipt_json, segments_json, annotation_status) = row
    return {"id": resume_id, "name": name, "family": family, "binding": binding,
            "application_id": application_id, "content_text": content_text,
            "lines": loads_json(lines_json, []), "archived": bool(archived),
            "content_hash": content_hash, "extraction_receipt": loads_json(receipt_json, {}),
            "segments": loads_json(segments_json, []), "annotation_status": annotation_status}


def list_resume_summaries(db_path: str, user_id: str, include_archived: bool = False) -> list[dict]:
    """Lightweight browser list that never returns resume text or segments."""
    condition = "" if include_archived else "AND resumes.archived = 0"
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT resumes.id, resumes.name, resumes.binding, resumes.application_id, "
            "applications.company, applications.position, resumes.archived, resumes.content_hash, "
            "length(resumes.content_text), resumes.annotation_status, resumes.updated_time "
            "FROM resumes LEFT JOIN applications ON applications.user_id = resumes.user_id "
            "AND applications.id = resumes.application_id "
            f"WHERE resumes.user_id = ? {condition} "
            "ORDER BY resumes.updated_time DESC, resumes.id DESC",
            (user_id,),
        ).fetchall()
    return [{
        "id": row[0], "name": row[1], "binding": row[2], "application_id": row[3],
        "application_company": row[4], "application_position": row[5],
        "archived": bool(row[6]), "content_hash": row[7], "character_count": row[8],
        "annotation_status": row[9], "updated_time": row[10],
    } for row in rows]


def get_active_resume_text(db_path: str, user_id: str, resume_id: int) -> dict | None:
    """Read only the corrected text needed by the explicit Library viewer/editor."""
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, name, content_text, content_hash, length(content_text), updated_time "
            "FROM resumes WHERE user_id = ? AND id = ? AND archived = 0",
            (user_id, resume_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "content_text": row[2],
        "content_hash": row[3],
        "character_count": row[4],
        "updated_time": row[5],
    }


def update_active_resume_text(
    db_path: str,
    user_id: str,
    resume_id: int,
    content_text: str,
    *,
    expected_content_hash: str,
) -> tuple[str, dict | None]:
    """CAS-save a user's corrected text and invalidate every derivative of the old text."""
    content_text = validate_resume_text(content_text)
    content_hash = resume_content_hash(content_text)
    segments = segment_resume_text(content_text)
    extraction_receipt = {
        "version": "resume-extraction-v1",
        "canonicalization": "crlf-to-lf",
        "character_count": len(content_text),
        "segment_count": len(segments),
        "content_hash": content_hash,
        "manually_corrected": True,
    }
    changed_time = now_iso()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT name, content_hash FROM resumes "
            "WHERE user_id = ? AND id = ? AND archived = 0",
            (user_id, resume_id),
        ).fetchone()
        if row is None:
            return "not_found", None
        if row[1] != expected_content_hash:
            return "stale", None
        if row[1] != content_hash:
            conn.execute(
                "UPDATE resumes SET content_text = ?, content_hash = ?, extraction_receipt_json = ?, "
                "segments_json = ?, lines_json = NULL, annotation_status = 'pending', updated_time = ? "
                "WHERE user_id = ? AND id = ? AND archived = 0",
                (
                    content_text,
                    content_hash,
                    json.dumps(extraction_receipt, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(segments, ensure_ascii=False, separators=(",", ":")),
                    changed_time,
                    user_id,
                    resume_id,
                ),
            )
            _invalidate_prep_for_resume(conn, user_id, resume_id)
        else:
            changed_time = conn.execute(
                "SELECT updated_time FROM resumes WHERE user_id = ? AND id = ?",
                (user_id, resume_id),
            ).fetchone()[0]
        return "ok", {
            "id": resume_id,
            "name": row[0],
            "content_text": content_text,
            "content_hash": content_hash,
            "character_count": len(content_text),
            "updated_time": changed_time,
        }


def list_resumes(db_path: str, user_id: str, include_archived: bool = False) -> list[dict]:
    """List resume versions newest first."""
    condition = "" if include_archived else "AND archived = 0"
    with read_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT id FROM resumes WHERE user_id = ? {condition} ORDER BY updated_time DESC",
            (user_id,),
        ).fetchall()
    return [get_resume(db_path, user_id, resume_id) for (resume_id,) in rows]


def archive_resume_with_file(db_path: str, user_id: str,
                             resume_id: int) -> tuple[bool, str | None]:
    """Archive a version and return its upload path for post-commit deletion."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT file_path FROM resumes WHERE user_id = ? AND id = ?", (user_id, resume_id)
        ).fetchone()
        if exists is None:
            return False, None
        file_path = exists[0]
        # Determine the latest fallback before setting archived, or applications
        # without explicit bindings would be missed.
        _invalidate_prep_for_resume(conn, user_id, resume_id)
        cursor = conn.execute(
            "UPDATE resumes SET archived = 1, file_path = NULL, updated_time = ? "
            "WHERE user_id = ? AND id = ?",
            (now_iso(), user_id, resume_id),
        )
        return cursor.rowcount > 0, file_path


def archive_resume(db_path: str, user_id: str, resume_id: int) -> bool:
    """Soft-archive a resume version without deleting its managed file."""
    archived, _file_path = archive_resume_with_file(db_path, user_id, resume_id)
    return archived


def pick_resume_for_application(db_path: str, user_id: str, application_id: int) -> dict | None:
    """Choose the explicit application resume or latest active fallback."""
    with read_connection(db_path) as conn:
        application = conn.execute(
            "SELECT resume_id FROM applications WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if application is None:
            return None
        bound_resume_id = application[0]
        if bound_resume_id is not None:
            bound = conn.execute(
                "SELECT id FROM resumes WHERE user_id = ? AND id = ? AND archived = 0",
                (user_id, bound_resume_id),
            ).fetchone()
        else:
            bound = None
        if bound is not None:
            resume_id = bound[0]
        else:
            newest = conn.execute(
                "SELECT id FROM resumes WHERE user_id = ? AND archived = 0 "
                "ORDER BY updated_time DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            resume_id = newest[0] if newest else None
    return get_resume(db_path, user_id, resume_id) if resume_id is not None else None
