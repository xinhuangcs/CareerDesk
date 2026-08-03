"""Test-only fixtures for already-published immutable question sets."""

import hashlib
import json

from careerdesk.platform.database import now_iso, transaction


def seed_question_set(
    db_path: str,
    user_id: str,
    question_ids: list[int],
    *,
    kind: str = "generated",
    resume_id: int | None = 1,
    material_fingerprint: str = "fixture-material",
    policy_fingerprint: str = "fixture-policy",
    repeat_scope: str = "global",
) -> int:
    """Insert a frozen set without exposing a production snapshot-creation path."""
    timestamp = now_iso()
    edition = "basic" if kind == "generated" else None
    if kind == "library_snapshot":
        resume_id = None
    with transaction(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, text, immutable_revision, category, channel, response_format, "
            "evaluation_kind, primary_competency, secondary_tags_json, rubric_json, "
            f"answer_guide_json, evidence_json FROM questions WHERE user_id = ? "
            f"AND id IN ({','.join('?' for _ in question_ids)}) ORDER BY id",
            (user_id, *question_ids),
        ).fetchall()
        assert len(rows) == len(set(question_ids))
        set_id = conn.execute(
            "INSERT INTO question_sets (user_id, kind, edition, resume_id, state, stage, generation, "
            "material_fingerprint, policy_fingerprint, generation_fingerprint, prompt_version, "
            "schema_version, rubric_version, segmentation_version, summary_policy_version, "
            "input_receipt_json, coverage_json, context_label, finished_time, created_time, updated_time) "
            "VALUES (?, ?, ?, ?, 'ready', 'ready', ?, ?, ?, ?, 'fixture-prompt', 'fixture-schema', "
            "'fixture-rubric', 'fixture-segments', 'fixture-summary', '{}', '{}', ?, ?, ?, ?)",
            (user_id, kind, edition, resume_id, f"fixture-{timestamp}", material_fingerprint,
             policy_fingerprint, hashlib.sha256(timestamp.encode()).hexdigest(),
             "历史题库练习" if kind == "library_snapshot" else "通用练习",
             timestamp, timestamp, timestamp),
        ).lastrowid
        for ordinal, row in enumerate(rows):
            rubric = row[9] or json.dumps({
                "essential_criteria": ["回答切题且具体"],
                "quality_signals": [],
                "critical_errors": [],
            }, ensure_ascii=False)
            guide = row[10] or json.dumps({"kind": "coaching_guide", "text": "说明依据"}, ensure_ascii=False)
            digest = hashlib.sha256(f"{row[0]}:{row[1]}".encode()).hexdigest()
            conn.execute(
                "INSERT INTO question_set_items (user_id, question_set_id, ordinal, "
                "canonical_question_id, canonical_revision, canonical_digest, text, category, "
                "channel, response_format, evaluation_kind, difficulty, primary_competency, "
                "secondary_tags_json, rubric_json, answer_authority, answer_guide_json, "
                "evidence_json, follow_up_allowed, repeat_scope, created_time) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intermediate', ?, ?, ?, "
                "'model_generated_unverified', ?, ?, ?, ?, ?)",
                (user_id, set_id, ordinal, row[0], row[2], digest, row[1],
                 row[3] or "professional_domain", row[4] or "interview",
                 row[5] or "oral_text", row[6] or "rubric", row[7] or "通用表达",
                 row[8] or "[]", rubric, guide, row[11] or "[]",
                 int((row[4] or "interview") == "interview"), repeat_scope, timestamp),
            )
    return set_id


__all__ = ["seed_question_set"]
