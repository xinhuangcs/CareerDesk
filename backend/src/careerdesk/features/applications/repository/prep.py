"""Application priority and preparation-generation lease lifecycle."""

import json
import math
from datetime import datetime, timezone
from typing import Callable, Iterable, Literal

from ....platform.database import (
    INTERACTIVE_BUSY_TIMEOUT_MS,
    loads_json,
    now_iso,
    read_connection,
    transaction,
)
from .shared import TimelineMutationConflict


# Reuse normal generation within its lease; only a later safe retry may take over.
PREP_JOB_LEASE_SECONDS = 6 * 60
_SEMANTICALLY_DERIVED_PREP_KEYS = frozenset(
    {
        "anchor",
        "error",
        "planner",
        "position_report",
        "prepared_time",
        "research",
        "research_attempt",
        "web_questions",
    }
)


def _invalidated_semantic_prep_json(prep_json: str | None) -> str | None:
    """Remove current-semantic fragments while preserving durable envelopes."""
    loaded = loads_json(prep_json, {})
    if not isinstance(loaded, dict):
        return None
    for key in _SEMANTICALLY_DERIVED_PREP_KEYS:
        loaded.pop(key, None)
    return json.dumps(loaded, ensure_ascii=False) if loaded else None


def _prep_retry_after_seconds(updated_time: str, *, lease_seconds: int = PREP_JOB_LEASE_SECONDS,
                              now: datetime | None = None) -> int:
    """Return seconds until takeover; malformed timestamps fail open."""
    try:
        touched = datetime.fromisoformat(updated_time)
        current = now or datetime.now(timezone.utc)
        age = max(0.0, (current - touched).total_seconds())
    except (TypeError, ValueError):
        return 0
    return max(0, math.ceil(lease_seconds - age))


def _invalidate_prep(conn, user_id: str, application_id: int) -> None:
    """Cancel stale work and invalidate fields instead of erasing all prep."""
    row = conn.execute(
        "SELECT prep_json FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        "UPDATE applications SET prep_status = 'none', prep_generation = NULL, "
        "prep_heartbeat_time = NULL, prep_json = ?, "
        "updated_time = ? WHERE user_id = ? AND id = ?",
        (
            _invalidated_semantic_prep_json(row[0]),
            now_iso(),
            user_id,
            application_id,
        ),
    )


def set_priority(
    db_path: str,
    user_id: str,
    application_id: int,
    priority: Literal["high", "medium", "low"] | None,
    *,
    expected_revision: int,
) -> bool:
    """Set or clear application priority, returning whether a row changed."""
    if priority not in {None, "high", "medium", "low"}:
        raise ValueError("岗位优先级必须是 high、medium、low 或 null")
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision FROM applications WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if row is None:
            return False
        if row[0] != expected_revision:
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
        changed = conn.execute(
            "UPDATE applications SET priority = ?, revision = revision + 1, "
            "updated_time = ? WHERE user_id = ? AND id = ? AND revision = ?",
            (
                priority,
                now_iso(),
                user_id,
                application_id,
                expected_revision,
            ),
        ).rowcount
        if changed != 1:  # pragma: no cover - BEGIN IMMEDIATE + revision preflight
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
        return True


def claim_prep_generation(db_path: str, user_id: str, application_id: int, generation: str, *,
                          force: bool = False,
                          restart_ready: bool = False,
                          lease_seconds: int = PREP_JOB_LEASE_SECONDS) -> dict:
    """Atomically claim preparation and return reuse/takeover lease state."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT prep_status, prep_heartbeat_time, updated_time, prep_json FROM applications "
            "WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if row is None:
            return {"status": "missing", "takeover": False, "retry_after_seconds": None}
        prep_status, prep_heartbeat_time, updated_time, prep_json = row
        running = prep_status in ("pending", "running")
        retry_after = _prep_retry_after_seconds(
            prep_heartbeat_time or updated_time, lease_seconds=lease_seconds)
        if running and (not force or retry_after > 0):
            return {
                "status": "running",
                "takeover": False,
                "retry_after_seconds": retry_after,
            }
        if prep_status == "ready" and (force or not restart_ready):
            # Reuse ready by default so a stale page cannot retrigger the paid
            # pipeline. Only explicit regeneration reruns ready work; force means
            # takeover of observed stale in-flight work and returns success if won.
            return {
                "status": "completed",
                "prep_status": prep_status,
                "takeover": False,
                "retry_after_seconds": None,
            }
        changed = now_iso()
        loaded = loads_json(prep_json, {})
        prep = loaded if isinstance(loaded, dict) else {}
        prep["research_attempt"] = _validated_research_attempt(
            {
                "attempt_state": "pending",
                "generation": generation,
                "updated_time": changed,
                "error_code": None,
            }
        )
        conn.execute(
            "UPDATE applications SET prep_status = 'pending', prep_generation = ?, "
            "prep_heartbeat_time = ?, prep_json = ?, updated_time = ? "
            "WHERE user_id = ? AND id = ?",
            (
                generation,
                changed,
                json.dumps(prep, ensure_ascii=False),
                changed,
                user_id,
                application_id,
            ),
        )
        return {
            "status": "started",
            "takeover": running,
            "retry_after_seconds": lease_seconds,
        }


def touch_prep_generation(db_path: str, user_id: str, application_id: int,
                          generation: str, *, company: str | None = None) -> bool:
    """Validate and renew the current lease; stale generations cannot renew."""
    with transaction(db_path) as conn:
        company_condition = " AND company = ?" if company is not None else ""
        params = [now_iso(), user_id, application_id, generation]
        if company is not None:
            params.append(company)
        cursor = conn.execute(
            "UPDATE applications SET prep_heartbeat_time = ? WHERE user_id = ? AND id = ? "
            "AND prep_status IN ('pending', 'running') AND prep_generation = ?"
            f"{company_condition}",
            params,
        )
        return cursor.rowcount == 1


def set_prep_status(db_path: str, user_id: str, application_id: int, status: str,
                    prep: dict | None = None, *, generation: str | None = None) -> bool:
    """Update prep state; generation guards writes and terminal state releases it."""
    with transaction(db_path) as conn:
        terminal = status in ("none", "ready", "failed")
        generation_clause = (
            " AND prep_generation = ?" if generation is not None
            else " AND prep_generation IS NULL"
        )
        params = [status, json.dumps(prep, ensure_ascii=False) if prep is not None else None,
                  1 if terminal else 0, None if terminal else now_iso(), now_iso(),
                  user_id, application_id]
        if generation is not None:
            params.append(generation)
        cursor = conn.execute(
            "UPDATE applications SET prep_status = ?, prep_json = COALESCE(?, prep_json), "
            "prep_generation = CASE WHEN ? THEN NULL ELSE prep_generation END, "
            "prep_heartbeat_time = ?, updated_time = ? "
            f"WHERE user_id = ? AND id = ?{generation_clause}",
            params,
        )
        return cursor.rowcount == 1


def merge_prep_artifacts(
    db_path: str,
    user_id: str,
    application_id: int,
    updates: dict,
    *,
    remove_keys: Iterable[str] = (),
    generation: str | None = None,
    terminal_status: str | None = None,
) -> bool:
    """Merge prep keys transactionally, preserving unknown keys and optional finish."""
    if not isinstance(updates, dict):
        raise TypeError("updates 必须是 dict")
    if terminal_status not in {None, "none", "ready", "failed"}:
        raise ValueError("terminal_status 必须是 none/ready/failed")
    removed = tuple(dict.fromkeys(remove_keys))
    if any(not isinstance(key, str) or not key for key in removed):
        raise ValueError("remove_keys 必须是非空字符串")
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        params: list = [user_id, application_id]
        generation_clause = " AND prep_generation IS NULL"
        if generation is not None:
            generation_clause = (
                " AND prep_generation = ? AND prep_status IN ('pending', 'running')"
            )
            params.append(generation)
        row = conn.execute(
            "SELECT prep_json FROM applications WHERE user_id = ? AND id = ?"
            f"{generation_clause}",
            params,
        ).fetchone()
        if row is None:
            return False
        loaded = loads_json(row[0], {})
        prep = loaded if isinstance(loaded, dict) else {}
        for key in removed:
            prep.pop(key, None)
        prep.update(updates)
        changed = now_iso()
        if terminal_status is None:
            cursor = conn.execute(
                "UPDATE applications SET prep_json = ?, updated_time = ? "
                "WHERE user_id = ? AND id = ?"
                f"{generation_clause}",
                [json.dumps(prep, ensure_ascii=False), changed, user_id, application_id,
                 *([generation] if generation is not None else [])],
            )
        else:
            cursor = conn.execute(
                "UPDATE applications SET prep_status = ?, prep_generation = NULL, "
                "prep_heartbeat_time = NULL, prep_json = ?, updated_time = ? "
                "WHERE user_id = ? AND id = ?"
                f"{generation_clause}",
                [terminal_status, json.dumps(prep, ensure_ascii=False), changed,
                 user_id, application_id,
                 *([generation] if generation is not None else [])],
            )
        return cursor.rowcount == 1


def merge_localized_prep_artifacts(
    db_path: str,
    user_id: str,
    application_id: int,
    content_locale: str,
    updates: dict,
    *,
    generation: str | None = None,
    terminal_status: str | None = None,
) -> bool:
    """Atomically merge one bounded locale slot without replacing the other language."""
    if content_locale not in {"zh-CN", "en"}:
        raise ValueError("unsupported content locale")
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        generation_clause = (
            " AND prep_generation = ?" if generation is not None
            else " AND prep_generation IS NULL"
        )
        params: list = [user_id, application_id]
        if generation is not None:
            params.append(generation)
        row = conn.execute(
            "SELECT prep_json FROM applications WHERE user_id = ? AND id = ?"
            f"{generation_clause}",
            params,
        ).fetchone()
        if row is None:
            return False
        loaded = loads_json(row[0], {})
        prep = loaded if isinstance(loaded, dict) else {}
        localized = prep.get("localized")
        localized = dict(localized) if isinstance(localized, dict) else {}
        entry = localized.get(content_locale)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(updates)
        entry["content_locale"] = content_locale
        localized[content_locale] = entry
        prep["localized"] = localized
        prep["active_content_locale"] = content_locale
        prep.pop("error", None)
        status_sql = ""
        sql_params: list = [json.dumps(prep, ensure_ascii=False), now_iso()]
        if terminal_status is not None:
            status_sql = ", prep_status = ?, prep_generation = NULL, prep_heartbeat_time = NULL"
            sql_params.append(terminal_status)
        sql_params.extend([user_id, application_id])
        if generation is not None:
            sql_params.append(generation)
        cursor = conn.execute(
            "UPDATE applications SET prep_json = ?, updated_time = ?"
            f"{status_sql} WHERE user_id = ? AND id = ?{generation_clause}",
            sql_params,
        )
        return cursor.rowcount == 1


def _freeze_resume_adaptation_input_in_transaction(
    conn,
    user_id: str,
    application_id: int,
    *,
    resume_reader: Callable,
    company_reader: Callable,
) -> dict:
    """Freeze the Applications-owned row plus public related-feature projections."""
    if not getattr(conn, "in_transaction", False):
        raise ValueError("adaptation input freeze requires an active transaction")
    row = conn.execute(
        "SELECT company, position, department, jd_parsed_json, jd_text, "
        "jd_content_hash, jd_receipt_json, jd_receipt_status, "
        "resume_id, prep_json, prep_status, revision "
        "FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone()
    if row is None:
        return {"status": "missing"}
    (
        company,
        position,
        department,
        jd_parsed_json,
        jd_text,
        jd_content_hash,
        jd_receipt_json,
        jd_receipt_status,
        bound_resume_id,
        prep_json,
        prep_status,
        revision,
    ) = row
    resume_projection = resume_reader(conn, user_id, bound_resume_id)
    company_projection = company_reader(conn, user_id, company)
    if not isinstance(resume_projection, dict) or not isinstance(company_projection, dict):
        raise TypeError("adaptation related-feature readers must return dict projections")
    loaded_prep = loads_json(prep_json, {})
    loaded_jd = loads_json(jd_parsed_json, {})
    return {
        "status": "ok",
        "application_id": application_id,
        "company": company,
        "position": position,
        "department": department,
        "jd_parsed": loaded_jd if isinstance(loaded_jd, dict) else {},
        "jd_text": jd_text,
        "jd_content_hash": jd_content_hash,
        "jd_receipt": loads_json(jd_receipt_json, None),
        "jd_receipt_status": jd_receipt_status,
        "bound_resume_id": bound_resume_id,
        "bound_resume": resume_projection.get("bound_resume"),
        "resumes": (
            resume_projection.get("resumes")
            if isinstance(resume_projection.get("resumes"), list)
            else []
        ),
        "prep": loaded_prep if isinstance(loaded_prep, dict) else {},
        "prep_status": prep_status,
        "edit_revision": revision,
        "company_aliases": (
            company_projection.get("aliases")
            if isinstance(company_projection.get("aliases"), list)
            else []
        ),
        "company_notes": company_projection.get("notes"),
    }


def freeze_resume_adaptation_input(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    resume_reader: Callable,
    company_reader: Callable,
) -> dict:
    """Return one stable SQLite snapshot spanning the aggregate's public readers."""
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        return _freeze_resume_adaptation_input_in_transaction(
            conn,
            user_id,
            application_id,
            resume_reader=resume_reader,
            company_reader=company_reader,
        )


_RESUME_ADAPTATION_KEYS = frozenset({
    "resume_adaptation",
    "resume_adaptation_summary",
})


def merge_resume_adaptation_key_if_current(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    key: Literal["resume_adaptation", "resume_adaptation_summary"],
    value: dict,
    expected_input_hash: str,
    current_validator: Callable[[dict, str], bool],
    resume_reader: Callable,
    company_reader: Callable,
    content_locale: Literal["zh-CN", "en"] | None = None,
) -> bool:
    """CAS-merge one adaptation key after re-freezing all semantic inputs.

    Applications owns the transaction and key merge.  The orchestration layer
    supplies a deterministic, side-effect-free validator because interpretation
    of the opaque adaptation input hash remains a Workflow responsibility.
    """
    if key not in _RESUME_ADAPTATION_KEYS:
        raise ValueError("unsupported resume adaptation prep key")
    if not isinstance(value, dict):
        raise TypeError("resume adaptation prep value must be a dict")
    if (
        not isinstance(expected_input_hash, str)
        or len(expected_input_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_input_hash)
    ):
        raise ValueError("expected_input_hash must be a lowercase SHA-256 hex digest")
    if not callable(current_validator):
        raise TypeError("current_validator must be callable")
    receipt_field = (
        "input_hash" if key == "resume_adaptation" else "resume_content_hash"
    )
    if value.get(receipt_field) != expected_input_hash:
        raise ValueError(f"{key}.{receipt_field} must match expected_input_hash")
    # Validate/detach the value before taking a write lock.  Raw resume/JD text
    # never enters this serialized artifact unless the caller explicitly puts
    # it in the task-owned value.
    persisted_value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _freeze_resume_adaptation_input_in_transaction(
            conn,
            user_id,
            application_id,
            resume_reader=resume_reader,
            company_reader=company_reader,
        )
        if current.get("status") != "ok":
            return False
        if not current_validator(current, expected_input_hash):
            return False
        prep = dict(current["prep"])
        if key == "resume_adaptation" and content_locale is not None:
            localized = prep.get("localized")
            localized = dict(localized) if isinstance(localized, dict) else {}
            entry = localized.get(content_locale)
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry[key] = persisted_value
            entry["content_locale"] = content_locale
            localized[content_locale] = entry
            prep["localized"] = localized
            prep["active_content_locale"] = content_locale
        else:
            prep[key] = persisted_value
        cursor = conn.execute(
            "UPDATE applications SET prep_json = ?, updated_time = ? "
            "WHERE user_id = ? AND id = ?",
            (
                json.dumps(prep, ensure_ascii=False),
                now_iso(),
                user_id,
                application_id,
            ),
        )
        return cursor.rowcount == 1


def _validated_research_attempt(attempt: dict) -> dict:
    # Delay import to avoid a module cycle between Applications public startup and
    # ResearchService; the Research feature still owns strict validation.
    from ...research.public import ResearchAttempt

    return ResearchAttempt.model_validate(attempt).model_dump(mode="json")


def set_research_attempt(
    db_path: str,
    user_id: str,
    application_id: int,
    attempt: dict,
    *,
    generation: str | None = None,
) -> bool:
    """Update only the latest research attempt without changing overall prep."""
    validated = _validated_research_attempt(attempt)
    if generation is not None and validated.get("generation") != generation:
        raise ValueError("research attempt generation 与写入 guard 不一致")
    return merge_prep_artifacts(
        db_path,
        user_id,
        application_id,
        {"research_attempt": validated},
        generation=generation,
    )


def publish_research_snapshot(
    db_path: str,
    user_id: str,
    application_id: int,
    snapshot: dict,
    *,
    generation: str,
    expected_semantic_claim: dict,
) -> bool:
    """Publish a complete snapshot after rechecking generation and semantic input."""
    import hashlib

    from ...research.public import ResearchSnapshot, research_semantic_claim

    validated_snapshot = ResearchSnapshot.model_validate(snapshot).model_dump(mode="json")
    expected_hash = hashlib.sha256(
        json.dumps(
            expected_semantic_claim,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if validated_snapshot["semantic_claim_hash"] != expected_hash:
        return False

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT a.company, a.position, a.department, a.jd_text, a.prep_json, "
            "c.aliases_json, c.notes "
            "FROM applications a LEFT JOIN companies c "
            "ON c.user_id = a.user_id AND c.name_key = a.company_key "
            "WHERE a.user_id = ? AND a.id = ? "
            "AND a.prep_generation = ? AND a.prep_status IN ('pending', 'running')",
            (user_id, application_id, generation),
        ).fetchone()
        if row is None:
            return False
        company, position, department, jd_text, prep_json, aliases_json, notes = row
        current_claim = research_semantic_claim(
            company=company,
            aliases=loads_json(aliases_json, []),
            notes=notes or "",
            department=department,
            position=position,
            jd_text=jd_text,
            output_locale=expected_semantic_claim.get("content_locale", "zh-CN"),
            search_profile=expected_semantic_claim.get("search_profile", {}),
        )
        if current_claim != expected_semantic_claim:
            return False
        loaded = loads_json(prep_json, {})
        prep = loaded if isinstance(loaded, dict) else {}
        succeeded = _validated_research_attempt(
            {
                "attempt_state": "succeeded",
                "generation": generation,
                "updated_time": now_iso(),
                "error_code": None,
            }
        )
        content_locale = validated_snapshot["content_locale"]
        localized = prep.get("localized")
        localized = dict(localized) if isinstance(localized, dict) else {}
        entry = localized.get(content_locale)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry["content_locale"] = content_locale
        entry["research_snapshot"] = validated_snapshot
        localized[content_locale] = entry
        prep["localized"] = localized
        prep["active_content_locale"] = content_locale
        prep["research_attempt"] = succeeded
        cursor = conn.execute(
            "UPDATE applications SET prep_json = ?, updated_time = ? "
            "WHERE user_id = ? AND id = ? AND prep_generation = ? "
            "AND prep_status IN ('pending', 'running')",
            (
                json.dumps(prep, ensure_ascii=False),
                now_iso(),
                user_id,
                application_id,
                generation,
            ),
        )
        return cursor.rowcount == 1


def fail_prep_generation(db_path: str, user_id: str, application_id: int, message: str, *,
                         generation: str | None = None) -> bool:
    """Record failure in current prep without overwriting the last successful work."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        generation_clause = (
            " AND prep_generation = ?" if generation is not None
            else " AND prep_generation IS NULL"
        )
        params: list = [user_id, application_id]
        if generation is not None:
            params.append(generation)
        row = conn.execute(
            "SELECT prep_json FROM applications WHERE user_id = ? AND id = ?"
            f"{generation_clause}",
            params,
        ).fetchone()
        if row is None:
            return False
        loaded = loads_json(row[0], {})
        prep = loaded if isinstance(loaded, dict) else {}
        prep["error"] = message
        attempt = prep.get("research_attempt")
        if isinstance(attempt, dict) and attempt.get("attempt_state") in {"pending", "running"}:
            if generation is None or attempt.get("generation") == generation:
                prep["research_attempt"] = _validated_research_attempt(
                    {
                        "attempt_state": "failed",
                        "generation": attempt.get("generation"),
                        "updated_time": now_iso(),
                        "error_code": "prep_failed",
                    }
                )
        conn.execute(
            "UPDATE applications SET prep_status = 'failed', prep_generation = NULL, "
            "prep_heartbeat_time = NULL, prep_json = ?, updated_time = ? "
            "WHERE user_id = ? AND id = ?"
            f"{generation_clause}",
            [json.dumps(prep, ensure_ascii=False), now_iso(), user_id, application_id,
             *([generation] if generation is not None else [])],
        )
        return True
