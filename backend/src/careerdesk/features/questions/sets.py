"""Persistence owner for immutable question sets and library snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ...platform.database import loads_json, now_iso, read_connection, transaction


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GenerationClaim:
    status: str
    question_set_id: int | None
    generation: str | None = None
    safe_error_code: str | None = None


def claim_generation(db_path: str, user_id: str, *, client_command_id: str,
                     request_digest: str, refresh: bool, metadata: dict) -> GenerationClaim:
    """Claim one replayable command and tenant-scoped single-flight generation."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        command = conn.execute(
            "SELECT request_digest, state, canonical_question_set_id, safe_error_code "
            "FROM question_set_commands "
            "WHERE user_id = ? AND client_command_id = ?", (user_id, client_command_id),
        ).fetchone()
        if command is not None:
            if command[0] != request_digest:
                raise ValueError("client_command_id 已用于不同请求")
            return GenerationClaim(command[1], command[2], safe_error_code=command[3])
        existing = None
        if not refresh:
            existing = conn.execute(
                "SELECT id FROM question_sets WHERE user_id = ? AND kind = 'generated' "
                "AND state = 'ready' AND archived_at IS NULL AND material_fingerprint = ? "
                "AND policy_fingerprint = ? "
                "AND COALESCE(json_extract(input_receipt_json, '$.content_locale'), 'zh-CN') = ? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, metadata["material_fingerprint"], metadata["policy_fingerprint"],
                 metadata["content_locale"]),
            ).fetchone()
        if existing is None:
            existing = conn.execute(
                "SELECT id FROM question_sets WHERE user_id = ? AND generation_fingerprint = ? "
                "AND state IN ('pending', 'running') ORDER BY id DESC LIMIT 1",
                (user_id, metadata["generation_fingerprint"]),
            ).fetchone()
        timestamp = now_iso()
        if existing is not None:
            state = conn.execute(
                "SELECT state FROM question_sets WHERE id = ?", (existing[0],),
            ).fetchone()[0]
            command_state = "completed" if state == "ready" else "running"
            conn.execute(
                "INSERT INTO question_set_commands (user_id, client_command_id, request_digest, "
                "state, canonical_question_set_id, created_time, updated_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, client_command_id, request_digest, command_state, existing[0], timestamp, timestamp),
            )
            return GenerationClaim(command_state, existing[0])
        cursor = conn.execute(
            "INSERT INTO question_sets (user_id, kind, edition, resume_id, application_id, "
            "state, stage, generation, material_fingerprint, policy_fingerprint, "
            "generation_fingerprint, prompt_version, schema_version, rubric_version, "
            "segmentation_version, summary_policy_version, model_label, input_receipt_json, "
            "coverage_json, context_label, started_time, created_time, updated_time) "
            "VALUES (?, 'generated', ?, ?, ?, 'running', 'preparing', ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, '{}', ?, ?, ?, ?)",
            (user_id, metadata["edition"], metadata["resume_id"], metadata.get("application_id"),
             metadata["generation"],
             metadata["material_fingerprint"], metadata["policy_fingerprint"],
             metadata["generation_fingerprint"], metadata["prompt_version"], metadata["schema_version"],
             metadata["rubric_version"], metadata["segmentation_version"],
             metadata["summary_policy_version"], metadata.get("model_label"),
             json.dumps(metadata["input_receipt"], separators=(",", ":")), metadata["context_label"],
             timestamp, timestamp, timestamp),
        )
        set_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO question_set_commands (user_id, client_command_id, request_digest, state, "
            "canonical_question_set_id, created_time, updated_time) VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (user_id, client_command_id, request_digest, set_id, timestamp, timestamp),
        )
        return GenerationClaim("running", set_id, metadata["generation"])


def update_generation_stage(db_path: str, user_id: str, set_id: int, generation: str, stage: str) -> bool:
    timestamp = now_iso()
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "UPDATE question_sets SET stage = ?, heartbeat_time = ?, updated_time = ? "
            "WHERE user_id = ? AND id = ? AND generation = ? AND state = 'running'",
            (stage, timestamp, timestamp, user_id, set_id, generation),
        )
        return cursor.rowcount == 1


def fail_generation(db_path: str, user_id: str, set_id: int, generation: str, code: str) -> None:
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE question_sets SET state = 'failed', stage = 'failed', safe_error_code = ?, "
            "finished_time = ?, updated_time = ? WHERE user_id = ? AND id = ? AND generation = ? "
            "AND state = 'running'", (code, timestamp, timestamp, user_id, set_id, generation),
        )
        conn.execute(
            "UPDATE question_set_commands SET state = 'failed', safe_error_code = ?, updated_time = ? "
            "WHERE user_id = ? AND canonical_question_set_id = ? AND state = 'running'",
            (code, timestamp, user_id, set_id),
        )


def publish_generation(db_path: str, user_id: str, set_id: int, generation: str, *,
                       expected_material_fingerprint: str, coverage: dict,
                       items: list[dict]) -> bool:
    """Atomically publish canonical rows and immutable playback items."""
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            "SELECT material_fingerprint FROM question_sets WHERE user_id = ? AND id = ? "
            "AND generation = ? AND state = 'running'", (user_id, set_id, generation),
        ).fetchone()
        if owner is None or owner[0] != expected_material_fingerprint:
            return False
        for ordinal, item in enumerate(items):
            encoded = {
                "secondary": json.dumps(item["secondary_tags"], ensure_ascii=False),
                "rubric": json.dumps(item["rubric"], ensure_ascii=False),
                "guide": json.dumps(item["answer_guide"], ensure_ascii=False),
                "evidence": json.dumps(item["evidence"], ensure_ascii=False),
            }
            cursor = conn.execute(
                "INSERT INTO questions (user_id, text, source, question_set_id, immutable_revision, "
                "category, channel, response_format, evaluation_kind, primary_competency, "
                "secondary_tags_json, rubric_json, answer_guide_json, evidence_json, status, "
                "created_time, updated_time) VALUES (?, ?, 'generated', ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'active', ?, ?)",
                (user_id, item["text"], set_id, item["category"], item["channel"],
                 item["response_format"], item["evaluation_kind"], item["primary_competency"],
                 encoded["secondary"], encoded["rubric"], encoded["guide"], encoded["evidence"],
                 timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO question_set_items (user_id, question_set_id, ordinal, canonical_question_id, "
                "canonical_revision, canonical_digest, text, category, channel, response_format, "
                "evaluation_kind, difficulty, primary_competency, secondary_tags_json, rubric_json, "
                "answer_authority, answer_guide_json, evidence_json, follow_up_allowed, repeat_scope, "
                "created_time) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, set_id, ordinal, cursor.lastrowid, canonical_hash(item), item["text"],
                 item["category"], item["channel"], item["response_format"], item["evaluation_kind"],
                 item["difficulty"], item["primary_competency"], encoded["secondary"], encoded["rubric"],
                 item["answer_authority"], encoded["guide"], encoded["evidence"],
                 int(item["follow_up_allowed"]), item["repeat_scope"], timestamp),
            )
        conn.execute(
            "UPDATE question_sets SET state = 'ready', stage = 'ready', coverage_json = ?, "
            "finished_time = ?, updated_time = ? WHERE user_id = ? AND id = ? AND generation = ? "
            "AND state = 'running'", (json.dumps(coverage, ensure_ascii=False), timestamp, timestamp,
                                      user_id, set_id, generation),
        )
        conn.execute(
            "UPDATE question_set_commands SET state = 'completed', updated_time = ? WHERE user_id = ? "
            "AND canonical_question_set_id = ? AND state = 'running'", (timestamp, user_id, set_id),
        )
    return True


def _item_from_row(row) -> dict:
    return {"id": row[0], "ordinal": row[1], "canonical_question_id": row[2], "text": row[3],
            "category": row[4], "channel": row[5], "response_format": row[6],
            "evaluation_kind": row[7], "difficulty": row[8], "primary_competency": row[9],
            "secondary_tags": loads_json(row[10], []), "rubric": loads_json(row[11], {}),
            "answer_authority": row[12], "answer_guide": loads_json(row[13], {}),
            "evidence": loads_json(row[14], []), "follow_up_allowed": bool(row[15]),
            "repeat_scope": row[16]}


def get_question_set(db_path: str, user_id: str, set_id: int, *, include_items: bool = False) -> dict | None:
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, kind, edition, resume_id, application_id, state, stage, "
            "safe_error_code, material_fingerprint, policy_fingerprint, input_receipt_json, "
            "coverage_json, context_label, archived_at, created_time, updated_time FROM question_sets "
            "WHERE user_id = ? AND id = ?", (user_id, set_id),
        ).fetchone()
        if row is None:
            return None
        receipt = loads_json(row[10], {})
        result = {"id": row[0], "kind": row[1], "edition": row[2], "resume_id": row[3],
                  "application_id": row[4], "state": row[5], "stage": row[6],
                  "safe_error_code": row[7], "material_fingerprint": row[8],
                  "policy_fingerprint": row[9], "input_receipt": receipt,
                  "content_locale": receipt.get("content_locale", "zh-CN"),
                  "coverage": loads_json(row[11], {}), "context_label": row[12],
                  "archived_at": row[13], "created_time": row[14], "updated_time": row[15]}
        result["question_count"] = conn.execute(
            "SELECT COUNT(*) FROM question_set_items WHERE question_set_id = ?", (set_id,),
        ).fetchone()[0]
        result["unpracticed_count"] = conn.execute(
            "SELECT COUNT(*) FROM question_set_items item WHERE item.question_set_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM grill_session_items session_item "
            "JOIN grill_answers answer ON answer.session_item_id = session_item.id "
            "WHERE session_item.user_id = ? AND session_item.question_set_item_id = item.id)",
            (set_id, user_id),
        ).fetchone()[0]
        if include_items:
            result["items"] = [_item_from_row(item) for item in conn.execute(
                "SELECT id, ordinal, canonical_question_id, text, category, channel, response_format, "
                "evaluation_kind, difficulty, primary_competency, secondary_tags_json, rubric_json, "
                "answer_authority, answer_guide_json, evidence_json, follow_up_allowed, repeat_scope "
                "FROM question_set_items WHERE user_id = ? AND question_set_id = ? ORDER BY ordinal",
                (user_id, set_id),
            ).fetchall()]
    return result


def list_question_sets(db_path: str, user_id: str) -> list[dict]:
    with read_connection(db_path) as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM question_sets WHERE user_id = ? ORDER BY id DESC", (user_id,),
        )]
    return [value for value in (get_question_set(db_path, user_id, item) for item in ids) if value]


def question_set_start_snapshot_in_transaction(conn, user_id: str, set_id: int) -> dict | None:
    """Return the immutable identity needed for an atomic start decision."""
    if not getattr(conn, "in_transaction", False):
        raise ValueError("question-set start snapshot requires an active transaction")
    row = conn.execute(
        "SELECT id, kind, edition, resume_id, application_id, state, "
        "material_fingerprint, policy_fingerprint, archived_at FROM question_sets "
        "WHERE user_id = ? AND id = ?",
        (user_id, set_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "kind": row[1], "edition": row[2], "resume_id": row[3],
        "application_id": row[4], "state": row[5],
        "material_fingerprint": row[6], "policy_fingerprint": row[7], "archived_at": row[8],
    }


def archive_or_delete_question_set(db_path: str, user_id: str, set_id: int) -> str | None:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM question_sets WHERE user_id = ? AND id = ?", (user_id, set_id)).fetchone() is None:
            return None
        if conn.execute("SELECT 1 FROM grill_sessions WHERE user_id = ? AND question_set_id = ? LIMIT 1",
                        (user_id, set_id)).fetchone():
            timestamp = now_iso()
            conn.execute("UPDATE question_sets SET archived_at = COALESCE(archived_at, ?), updated_time = ? "
                         "WHERE user_id = ? AND id = ?", (timestamp, timestamp, user_id, set_id))
            return "archived"
        canonical_ids = [row[0] for row in conn.execute(
            "SELECT canonical_question_id FROM question_set_items WHERE question_set_id = ? "
            "AND canonical_question_id IS NOT NULL", (set_id,))]
        conn.execute("DELETE FROM question_set_items WHERE question_set_id = ?", (set_id,))
        conn.execute("DELETE FROM question_set_commands WHERE canonical_question_set_id = ?", (set_id,))
        for question_id in canonical_ids:
            conn.execute("DELETE FROM questions WHERE user_id = ? AND id = ? AND source = 'generated' "
                         "AND NOT EXISTS (SELECT 1 FROM question_set_items WHERE canonical_question_id = ?)",
                         (user_id, question_id, question_id))
        conn.execute("DELETE FROM question_sets WHERE user_id = ? AND id = ?", (user_id, set_id))
        return "deleted"


def recover_running_generations(db_path: str) -> int:
    timestamp = now_iso()
    with transaction(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM question_sets WHERE state = 'running'").fetchone()[0]
        conn.execute("UPDATE question_sets SET state = 'failed', stage = 'failed', "
                     "safe_error_code = 'outcome_unknown', finished_time = ?, updated_time = ? "
                     "WHERE state = 'running'", (timestamp, timestamp))
        conn.execute("UPDATE question_set_commands SET state = 'failed', safe_error_code = 'outcome_unknown', "
                     "updated_time = ? WHERE state = 'running'", (timestamp,))
        return count


__all__ = ["GenerationClaim", "archive_or_delete_question_set", "canonical_hash",
           "claim_generation", "fail_generation", "get_question_set",
           "list_question_sets", "publish_generation", "recover_running_generations",
           "question_set_start_snapshot_in_transaction", "update_generation_stage"]
